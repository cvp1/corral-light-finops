"""RSASSA-PKCS1-v1_5 with SHA-256, in the standard library only, for the
Google service-account token (RFC 8017 §8.2). Signing only, from a PEM
"PRIVATE KEY" (PKCS#8) or "RSA PRIVATE KEY" (PKCS#1), as Google issues.

The private key never leaves this process; the core also refuses a result
that contains it.
"""
import base64
import hashlib
import re

# DER of DigestInfo for SHA-256, before the 32-byte digest (RFC 8017 §9.2).
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _der(buf, i):
    """-> (tag, value bytes, next index) for one DER element at buf[i]."""
    if i + 2 > len(buf):
        raise ValueError("truncated DER")
    tag = buf[i]
    n = buf[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        if not 1 <= k <= 4:
            raise ValueError("unsupported DER length")
        n = int.from_bytes(buf[i:i + k], "big")
        i += k
    if i + n > len(buf):
        raise ValueError("truncated DER")
    return tag, buf[i:i + n], i + n


def _seq(buf):
    tag, body, _ = _der(buf, 0)
    if tag != 0x30:
        raise ValueError("expected a DER SEQUENCE")
    out, i = [], 0
    while i < len(body):
        t, v, i = _der(body, i)
        out.append((t, v))
    return out


def private_key(pem):
    """PEM text -> (n, d, p, q, dp, dq, qinv) as ints."""
    m = re.search(r"-----BEGIN (RSA )?PRIVATE KEY-----(.+?)-----END (RSA )?PRIVATE KEY-----",
                  pem, re.S)
    if not m:
        raise ValueError("no PEM private key found")
    der = base64.b64decode("".join(m.group(2).split()))
    parts = _seq(der)
    if m.group(1) is None:                       # PKCS#8: version, algorithm, key
        if len(parts) < 3 or parts[2][0] != 0x04:
            raise ValueError("not a PKCS#8 private key")
        parts = _seq(parts[2][1])
    ints = [int.from_bytes(v, "big") for t, v in parts if t == 0x02]
    if len(ints) < 9:
        raise ValueError("not an RSA private key")
    _ver, n, _e, d, p, q, dp, dq, qinv = ints[:9]
    return n, d, p, q, dp, dq, qinv


def sign(pem, message):
    """-> the PKCS#1 v1.5 SHA-256 signature of `message` (bytes)."""
    n, d, p, q, dp, dq, qinv = private_key(pem)
    k = (n.bit_length() + 7) // 8
    t = SHA256_PREFIX + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        raise ValueError("the RSA key is too short")
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    m = int.from_bytes(em, "big")
    # Chinese remainder theorem: two half-size exponentiations, recombined.
    s1, s2 = pow(m, dp, p), pow(m, dq, q)
    s = s2 + q * ((qinv * (s1 - s2)) % p)
    if s >= n:
        raise ValueError("bad CRT recombination")
    return s.to_bytes(k, "big")
