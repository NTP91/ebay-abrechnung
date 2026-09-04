"""eBay GET signatures (RFC 9421), using locally configured Ed25519 keys."""
import base64
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def signing_headers(url, config, created=None):
    """No payload: Content-Digest is not required. @path excludes the query per RFC 9421."""
    try:
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.netloc != 'apiz.ebay.com' or not parts.path.startswith('/sell/finances/v1/'):
            raise ValueError()
        stamp = int(time.time() if created is None else created)
        if int(config.get('signing_expiration', 0)) <= stamp:
            raise ValueError()
        jwe = config['signing_jwe']
        if not isinstance(jwe, str) or len(jwe.split('.')) != 5 or any(c.isspace() for c in jwe):
            raise ValueError()
        value = config['signing_private_key'].encode('ascii')
        key = serialization.load_pem_private_key(value, password=None) if value.startswith(b'-----BEGIN') else serialization.load_der_private_key(base64.b64decode(value, validate=True), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError()
        parameters = '("x-ebay-signature-key" "@method" "@path" "@authority");created=' + str(stamp)
        base = '\n'.join(['"x-ebay-signature-key": ' + jwe, '"@method": GET',
                          '"@path": ' + parts.path, '"@authority": ' + parts.netloc,
                          '"@signature-params": ' + parameters])
        signed = base64.b64encode(key.sign(base.encode('ascii'))).decode('ascii')
        return {'x-ebay-signature-key': jwe, 'Signature-Input': 'sig1=' + parameters,
                'Signature': 'sig1=:' + signed + ':'}
    except Exception:
        raise ValueError('eBay-Signaturschluessel fehlen, sind abgelaufen oder ungueltig. Keine unsignierte Finances-Anfrage gesendet.') from None
