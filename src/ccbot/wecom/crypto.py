"""WeCom callback message encryption/decryption.

Implements the WeCom callback verification protocol:
- URL verification: decrypt echostr and return plain text
- Message decryption: AES-CBC decrypt incoming XML messages
- Message encryption: AES-CBC encrypt outgoing reply XML
- Signature verification: SHA1(token, timestamp, nonce, encrypt_msg)

Reference: https://developer.work.weixin.qq.com/document/path/90968
"""

import base64
import hashlib
import random
import string
import struct
import time
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WeComCrypto:
    """WeCom message encryption/decryption handler."""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str) -> None:
        self.token = token
        self.corp_id = corp_id
        # EncodingAESKey is base64-encoded, 43 chars -> 32 bytes AES key
        self.aes_key = base64.b64decode(encoding_aes_key + "=")
        self.iv = self.aes_key[:16]

    def verify_signature(
        self, signature: str, timestamp: str, nonce: str, encrypt_msg: str = ""
    ) -> bool:
        """Verify callback signature."""
        parts = sorted([self.token, timestamp, nonce, encrypt_msg])
        sha1 = hashlib.sha1("".join(parts).encode()).hexdigest()
        return sha1 == signature

    def decrypt(self, encrypted: str) -> str:
        """Decrypt an encrypted message string.

        The decrypted format is:
        random(16 bytes) + msg_len(4 bytes, network order) + msg + corp_id
        """
        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv))
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(base64.b64decode(encrypted)) + decryptor.finalize()

        # Remove PKCS#7 padding (32-byte block size, not 16)
        pad_val = decrypted[-1]
        if pad_val < 1 or pad_val > 32:
            raise ValueError(f"Invalid padding value: {pad_val}")
        decrypted = decrypted[:-pad_val]

        # Parse: 16 bytes random + 4 bytes msg_len + msg + corp_id
        msg_len = struct.unpack("!I", decrypted[16:20])[0]
        msg = decrypted[20 : 20 + msg_len].decode("utf-8")
        from_corp_id = decrypted[20 + msg_len :].decode("utf-8")

        if from_corp_id != self.corp_id:
            raise ValueError(
                f"Corp ID mismatch: expected {self.corp_id}, got {from_corp_id}"
            )

        return msg

    def encrypt(self, msg: str) -> str:
        """Encrypt a message string for reply."""
        msg_bytes = msg.encode("utf-8")
        # random(16) + msg_len(4) + msg + corp_id
        random_bytes = "".join(
            random.choices(string.ascii_letters + string.digits, k=16)
        ).encode()
        msg_len = struct.pack("!I", len(msg_bytes))
        plaintext = random_bytes + msg_len + msg_bytes + self.corp_id.encode()

        # Pad to 32-byte alignment (WeCom convention)
        pad_len = 32 - (len(plaintext) % 32)
        padded = plaintext + b" " * pad_len

        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv))
        encryptor = cipher.encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()

        return base64.b64encode(encrypted).decode()

    def decrypt_callback_xml(self, xml_body: str) -> str:
        """Extract and decrypt the Encrypt field from callback XML."""
        root = ET.fromstring(xml_body)
        encrypt_node = root.find("Encrypt")
        if encrypt_node is None or not encrypt_node.text:
            raise ValueError("Missing Encrypt element in callback XML")
        return self.decrypt(encrypt_node.text)

    def extract_encrypt_from_xml(self, xml_body: str) -> str:
        """Extract the raw Encrypt field (for signature verification)."""
        root = ET.fromstring(xml_body)
        encrypt_node = root.find("Encrypt")
        if encrypt_node is None or not encrypt_node.text:
            raise ValueError("Missing Encrypt element in callback XML")
        return encrypt_node.text

    def make_encrypted_reply(self, reply_msg: str) -> str:
        """Build an encrypted XML reply."""
        encrypted = self.encrypt(reply_msg)
        timestamp = str(int(time.time()))
        nonce = "".join(random.choices(string.digits, k=10))

        parts = sorted([self.token, timestamp, nonce, encrypted])
        signature = hashlib.sha1("".join(parts).encode()).hexdigest()

        return (
            "<xml>"
            f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>"
        )
