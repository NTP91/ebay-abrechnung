"""Central eBay transport. Business endpoints are GET-only; OAuth refresh is the sole POST."""
import time
from urllib.parse import quote

import requests


class EbayError(Exception):
    pass


def secrets_config():
    import streamlit as st
    try:
        values = dict(st.secrets['ebay_durchstart'])
        required = ('client_id', 'client_secret', 'ru_name', 'refresh_token')
        if not all(isinstance(values.get(k), str) and values[k].strip() for k in required):
            raise ValueError()
        return {k: values[k] for k in required}
    except Exception:
        raise EbayError('Zugangsdaten nicht verfügbar: Streamlit-Abschnitt ebay_durchstart mit client_id, client_secret, ru_name und refresh_token erforderlich.') from None


# No caller-supplied URLs, request methods, headers or OAuth scopes.
ENDPOINTS = {
    'standards': ('https://api.ebay.com', '/sell/analytics/v1/seller_standards_profile'),
    'metrics': ('https://api.ebay.com', '/sell/analytics/v1/customer_service_metric/{id}'),
    'returns': ('https://api.ebay.com', '/post-order/v2/return/search'),
    'disputes': ('https://apiz.ebay.com', '/sell/fulfillment/v1/payment_dispute_summary'),
    'transactions': ('https://apiz.ebay.com', '/sell/finances/v1/transaction'),
    'payouts': ('https://apiz.ebay.com', '/sell/finances/v1/payout'),
    'payout': ('https://apiz.ebay.com', '/sell/finances/v1/payout/{id}'),
    'funds': ('https://apiz.ebay.com', '/sell/finances/v1/seller_funds_summary'),
}


class Client:
    def __init__(self, provider=secrets_config, session=None, clock=time.time):
        self._provider = provider
        self._session = session or requests.Session()
        self._clock = clock
        self._token = None
        self._expires = 0
        self._cooldown = 0
        self._sensitive = []

    def __repr__(self):
        return '<eBay read-only client>'

    def _request(self, method, url, **kwargs):
        if self._clock() < self._cooldown:
            raise EbayError('Rate Limit: Bitte später erneut abrufen.')
        try:
            return self._session.request(method, url, timeout=(10, 25), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise EbayError('Verbindung fehlgeschlagen oder Zeitüberschreitung. Keine vollständigen Daten empfangen.') from None

    def _check(self, response, oauth=False):
        status = response.status_code
        if status == 429:
            try:
                delay = max(60, min(int(response.headers.get('Retry-After', 60)), 86400))
            except (ValueError, TypeError):
                delay = 60
            self._cooldown = self._clock() + delay
            raise EbayError('Rate Limit (HTTP 429): Bitte später erneut abrufen.')
        if not 200 <= status < 300:
            label = 'OAuth-Tokenfehler' if oauth else 'Berechtigung/Scope oder API-Zugangsanforderung fehlt' if status == 403 else 'API-Fehler'
            raise EbayError(f'{label} (HTTP {status}).')
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise EbayError('API-Antwort enthält kein gültiges JSON-Objekt.') from None

    def _refresh(self):
        cfg = self._provider()
        self._sensitive = list(cfg.values())
        response = self._request('POST', 'https://api.ebay.com/identity/v1/oauth2/token',
                                 auth=(cfg['client_id'], cfg['client_secret']),
                                 data={'grant_type': 'refresh_token', 'refresh_token': cfg['refresh_token']})
        data = self._check(response, oauth=True)
        try:
            token = data['access_token']
            seconds = int(data['expires_in'])
            if not isinstance(token, str) or not token or seconds <= 0:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise EbayError('OAuth-Antwort unvollständig; kein nutzbares Access Token.') from None
        self._token = token
        self._sensitive.append(token)
        self._expires = self._clock() + max(1, seconds - 60)

    def redact(self, value):
        if isinstance(value, dict):
            return {k: self.redact(v) for k, v in value.items()
                    if not any(word in k.casefold() for word in ('token', 'secret', 'authorization', 'password', 'client_id', 'ru_name'))}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            for secret in self._sensitive:
                if secret:
                    value = value.replace(secret, '[entfernt]')
        return value

    def get(self, endpoint, identifier='', params=None):
        if endpoint not in ENDPOINTS:
            raise EbayError('Nicht freigegebener Lese-Endpunkt.')
        host, path = ENDPOINTS[endpoint]
        if endpoint == 'metrics':
            if identifier not in {f'{kind}/{cycle}' for kind in ('ITEM_NOT_RECEIVED', 'ITEM_NOT_AS_DESCRIBED') for cycle in ('CURRENT', 'PROJECTED')}:
                raise EbayError('Unbekannte Service-Metrik.')
            path = path.replace('{id}', identifier)
        else:
            path = path.replace('{id}', quote(str(identifier), safe=''))
        for attempt in range(2):
            if not self._token or self._clock() >= self._expires:
                self._refresh()
            prefix = 'IAF ' if endpoint == 'returns' else 'Bearer '
            response = self._request('GET', host + path, params=params or {}, headers={
                'Authorization': prefix + self._token, 'Accept': 'application/json',
                'X-EBAY-C-MARKETPLACE-ID': 'EBAY_DE'})
            if response.status_code == 401 and attempt == 0:
                self._token = None
                continue
            return self.redact(self._check(response))
        raise EbayError('Authentifizierung fehlgeschlagen.')

    def pages(self, endpoint, collection, params=None, max_pages=100):
        result, pages, seen = [], [], set()
        identities, expected_total = set(), None
        offset, limit = 0, 100
        for _ in range(max_pages):
            data = self.get(endpoint, params={**(params or {}), 'limit': limit, 'offset': offset})
            total = data.get('paginationOutput', {}).get('totalEntries') if endpoint == 'returns' else data.get('total')
            rows = data.get(collection)
            if rows is None and total == 0:
                rows = []
            if not isinstance(rows, list):
                raise EbayError('API-Antwort unvollständig: erwartete Ergebnisliste fehlt.')
            key_fields = {'returns': ('returnId',), 'disputes': ('paymentDisputeId',),
                          'transactions': ('transactionId', 'transactionType'), 'payouts': ('payoutId',)}[endpoint]
            for row in rows:
                if not isinstance(row, dict) or not all(row.get(k) for k in key_fields):
                    raise EbayError('API-Antwort unvollständig: eindeutige Vorgangs-ID fehlt.')
                key = tuple(str(row[k]) for k in key_fields)
                if key in identities:
                    raise EbayError('Unvollständiger Abruf: API wiederholt Vorgänge über mehrere Seiten.')
                identities.add(key)
            import json
            signature = json.dumps(rows, sort_keys=True)
            if rows and signature in seen:
                raise EbayError('Unvollständiger Abruf: API wiederholt dieselbe Ergebnisseite.')
            seen.add(signature)
            result.extend(rows)
            pages.append(data)
            offset += len(rows)
            if total is not None:
                try:
                    total = int(total)
                    if (expected_total is not None and total != expected_total) or offset > total or total < 0:
                        raise ValueError()
                    expected_total = total
                    done = offset == total
                except (ValueError, TypeError):
                    raise EbayError('Ungültige API-Seitenzählung.') from None
            else:
                done = not data.get('next') and len(rows) < limit
            if done:
                return {'items': result, 'pages': pages}
            if not rows:
                raise EbayError('Unvollständiger Abruf: Ergebnisseite fehlt.')
        raise EbayError('Abrufgrenze erreicht; Daten sind nicht vollständig.')
