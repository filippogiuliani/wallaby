"""Link with Kooka: the weekly menu's foods go to a Kooka shopping list (Kooka's API is simulated)."""
import requests

import api_kooka
from conftest import PASSWORD, invite_token, ok


class FakeKooka:
    """Answers like Kooka's API: /api/lists and /api/lists/<id>/items."""

    def __init__(self):
        self.calls = []
        self.token = 'dt_good'
        self.down = False

    def __call__(self, method, url, json=None, headers=None, **kw):
        self.calls.append((method, url, json, headers))
        if self.down:
            raise requests.ConnectionError()
        r = requests.Response()
        r.headers['Content-Type'] = 'application/json'
        if headers['Authorization'] != 'Bearer ' + self.token:
            r.status_code, r._content = 401, b'{"error": "Sign-in required"}'
        elif url.endswith('/api/lists'):
            r.status_code = 200
            r._content = (b'[{"id": 3, "name": "Spesa", "kind": "checklist", "archived": false, "template": false},'
                          b' {"id": 4, "name": "Bagno", "kind": "project", "archived": false, "template": false}]')
        elif url.endswith('/api/lists/3/items'):
            r.status_code = 201
            r._content = b'{"added": [{"id": 1}, {"id": 2}], "skipped": ["Mela"]}'
        else:
            r.status_code, r._content = 404, b'{}'
        return r


def test_connect_and_send(family, client, monkeypatch):
    fake = FakeKooka()
    monkeypatch.setattr(api_kooka.requests, 'request', fake)
    c = family.c
    assert ok(c.get('/api/integrations/kooka')) == {'connected': False}
    assert c.put('/api/integrations/kooka', json={'url': 'ftp://x', 'token': 'dt_good'}).status_code == 400
    assert c.put('/api/integrations/kooka', json={'url': 'https://kooka.example', 'token': 'dt_bad'}).status_code == 502
    r = ok(c.put('/api/integrations/kooka', json={'url': 'https://kooka.example/api/', 'token': 'dt_good'}))
    # Only shopping-style lists; the only one is chosen
    assert r['lists'] == [{'id': 3, 'name': 'Spesa'}] and (r['list_id'], r['list_name'], r['url']) == (3, 'Spesa', 'https://kooka.example')
    status = ok(c.get('/api/integrations/kooka'))
    assert status['connected'] and 'token' not in status  # the token never goes back to the browser

    sent = ok(c.post('/api/integrations/kooka/send', json={'items': ['Zucchine', 'Mela', 'Ricotta']}))
    assert sent == {'added': 2, 'skipped': 1, 'list_name': 'Spesa'}
    method, url, payload, headers = fake.calls[-1]
    assert (method, url, payload) == ('POST', 'https://kooka.example/api/lists/3/items', {'items': ['Zucchine', 'Mela', 'Ricotta']})
    assert headers['Authorization'] == 'Bearer dt_good'
    assert c.post('/api/integrations/kooka/send', json={'items': []}).status_code == 400
    fake.down = True
    assert c.post('/api/integrations/kooka/send', json={'items': ['x']}).status_code == 502

    # Only the admin connects; members can send
    member = client()
    ok(member.post('/api/signup', json={'name': 'Sara', 'email': 'sara@example.com', 'password': PASSWORD, 'consent': True,
                                         'invite': invite_token(c)}), 201)
    assert member.put('/api/integrations/kooka', json={'url': 'https://x.example', 'token': 'dt_good'}).status_code == 403
    assert member.delete('/api/integrations/kooka').status_code == 403
    ok(c.delete('/api/integrations/kooka'))
    assert c.post('/api/integrations/kooka/send', json={'items': ['x']}).status_code == 400


def test_through_wombat_no_address_or_token(family, client, monkeypatch):
    """With Wombat the family's Kooka is reached through Wombat: the admin only picks the list."""
    import db
    from famkit import hub
    monkeypatch.setattr(hub, 'URL', 'http://wombat.test')
    monkeypatch.setattr(hub, 'KEY', 'k' * 40)
    with db.transaction() as conn:
        conn.execute('UPDATE families SET hub_id = 7')
        conn.execute('UPDATE users SET hub_id = 10')
    calls = []

    def fake(method, url, json=None, params=None, headers=None, **kw):
        calls.append((method, url, json, params, headers))
        r = requests.Response()
        r.headers['Content-Type'] = 'application/json'
        if url.endswith('/families/7/lists'):
            r.status_code, r._content = 200, b'[{"id": 3, "name": "Spesa"}, {"id": 5, "name": "Mercato"}]'
        elif url.endswith('/families/7/lists/5/items'):
            r.status_code, r._content = 201, b'{"added": [{"id": 1}], "skipped": ["Mela"]}'
        else:
            r.status_code, r._content = 404, b'{"error": "Kooka does not know this family yet: sign in to Kooka once"}'
        return r

    monkeypatch.setattr(api_kooka.requests, 'request', fake)
    c = family.c
    assert ok(c.get('/api/integrations/kooka')) == {'connected': True, 'via_hub': True, 'list_id': None, 'list_name': None}
    assert ok(c.get('/api/integrations/kooka/lists')) == [{'id': 3, 'name': 'Spesa'}, {'id': 5, 'name': 'Mercato'}]
    assert c.post('/api/integrations/kooka/send', json={'items': ['x']}).status_code == 400   # no list chosen yet
    r = ok(c.put('/api/integrations/kooka', json={'list_id': 5}))
    assert (r['list_id'], r['list_name'], r['via_hub']) == (5, 'Mercato', True)
    sent = ok(c.post('/api/integrations/kooka/send', json={'items': ['Zucchine', 'Mela']}))
    assert sent == {'added': 1, 'skipped': 1, 'list_name': 'Mercato'}
    method, url, payload, params, headers = calls[-1]
    assert (method, url, payload, params) == ('POST', 'http://wombat.test/api/service/apps/kooka/families/7/lists/5/items',
                                              {'items': ['Zucchine', 'Mela']}, {'user': 10})
    assert headers['Authorization'] == 'Bearer ' + 'k' * 40
    # Kooka has never seen the family: a readable reason
    with db.transaction() as conn:
        conn.execute('UPDATE kooka_links SET list_id = 6')
    r = c.post('/api/integrations/kooka/send', json={'items': ['x']})
    assert r.status_code == 502 and 'entra una volta in Kooka' in r.get_json()['error']
