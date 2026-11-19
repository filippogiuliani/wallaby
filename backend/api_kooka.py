"""Link with Kooka (the family organizer): the foods of the weekly menu go to a Kooka shopping list.

With Wombat (WALLABY_HUB_URL/KEY set and the family coming from Wombat) there is nothing to set up: Wallaby asks Wombat
(famkit.hub.ask),
which passes the request on to Kooka with the key it already has; the admin only picks the list. Entries are added
in Kooka as the person who sent them.

Without Wombat the family admin pastes the Kooka address and an API token created in Kooka (Family page →
Integrations), then picks the list. The token stays on this server: the browser never sees it."""
from urllib.parse import urlparse

import requests
from flask import Blueprint, abort, g, jsonify

import db
from famkit import hub
from auth import require_admin
from common import body, text

bp = Blueprint('kooka', __name__)
TIMEOUT = 10


def _link(conn):
    return conn.execute('SELECT * FROM kooka_links WHERE family_id = ?', (g.family_id,)).fetchone()


def _hub_family(conn):
    """Wombat's id of the current family when Kooka is reached through Wombat, else None."""
    if not hub.enabled():
        return None
    r = conn.execute('SELECT hub_id FROM families WHERE id = ?', (g.family_id,)).fetchone()
    return r['hub_id'] if r and r['hub_id'] else None


def _user_hub_id():
    with db.transaction() as conn:
        r = conn.execute('SELECT hub_id FROM users WHERE id = ?', (g.user['id'],)).fetchone()
    return r['hub_id'] if r else None


def _via_hub(hub_family, method, path, payload=None):
    """Kooka through Wombat (famkit.hub.ask)."""
    try:
        return hub.ask('kooka', method, path, hub_family, _user_hub_id(), payload, timeout=TIMEOUT + 2)
    except hub.AskError as e:
        abort(502, 'Kooka is not connected to Wombat' if 'not connected' in e.message
              else 'Kooka does not know this family yet: sign in to Kooka once' if 'sign in' in e.message
              else 'Kooka answered with an error')
    except hub.HubDown:
        abort(502, 'Wombat is not reachable')
    except hub.Unavailable as e:
        abort(502, 'Kooka is not reachable' if 'not reachable' in str(e) else 'Kooka answered with an error')


def _call(url, token, method, path, payload=None):
    """A request to Kooka's API; errors become a 502 with a readable reason."""
    try:
        r = requests.request(method, f"{url}/api{path}", json=payload, timeout=TIMEOUT, allow_redirects=False,
                             headers={'Authorization': f'Bearer {token}', 'User-Agent': 'Wallaby'})
    except requests.RequestException:
        abort(502, 'Kooka is not reachable')
    if r.status_code == 401:
        abort(502, 'Kooka refused the token')
    if r.status_code >= 400:
        abort(502, 'Kooka answered with an error')
    try:
        return r.json()
    except ValueError:
        abort(502, 'Kooka answered with an error')


def _url(value):
    url = text(value, 300).rstrip('/')
    if url.endswith('/api'):
        url = url[:-4]
    p = urlparse(url)
    if p.scheme not in ('http', 'https') or not p.hostname:
        abort(400, 'Invalid address')
    return url


def _lists(url, token):
    return [{'id': x['id'], 'name': x['name']} for x in _call(url, token, 'GET', '/lists')
            if x.get('kind') == 'checklist' and not x.get('archived') and not x.get('template')]


def _choose(lists, wanted, current):
    ids = [x['id'] for x in lists]
    list_id = wanted if wanted in ids else (current['list_id'] if current else None)
    if list_id not in ids:
        list_id = lists[0]['id'] if len(lists) == 1 else None
    return list_id, next((x['name'] for x in lists if x['id'] == list_id), None)


def _save(url, token, list_id, list_name):
    with db.transaction() as conn:
        conn.execute('''INSERT INTO kooka_links (family_id, url, token, list_id, list_name) VALUES (?,?,?,?,?)
                        ON CONFLICT(family_id) DO UPDATE SET url = excluded.url, token = excluded.token,
                        list_id = excluded.list_id, list_name = excluded.list_name''', (g.family_id, url, token, list_id, list_name))


@bp.get('/integrations/kooka')
def status():
    with db.transaction() as conn:
        link = _link(conn)
        hub_family = _hub_family(conn)
    if hub_family:
        # Through Wombat: always available, only the list is chosen here
        return jsonify(connected=True, via_hub=True, list_id=link['list_id'] if link else None,
                       list_name=link['list_name'] if link else None)
    if not link or not link['token']:
        return jsonify(connected=False)
    return jsonify(connected=True, url=link['url'], list_id=link['list_id'], list_name=link['list_name'])


@bp.get('/integrations/kooka/lists')
def lists_via_hub():
    """Through Wombat: the shopping lists to choose from."""
    require_admin()
    with db.transaction() as conn:
        hub_family = _hub_family(conn)
    if not hub_family:
        abort(404, 'Not available')
    return jsonify(_hub_lists(hub_family))


def _hub_lists(hub_family):
    data = _via_hub(hub_family, 'GET', '/lists')
    return [{'id': x['id'], 'name': x['name']} for x in data if isinstance(x, dict)] if isinstance(data, list) else []


@bp.put('/integrations/kooka')
def connect():
    """Save address and token (checked against Kooka); returns the shopping lists to choose from.
    Through Wombat only {list_id} is used."""
    require_admin()
    d = body()
    with db.transaction() as conn:
        current = _link(conn)
        hub_family = _hub_family(conn)
    if hub_family:
        lists = _hub_lists(hub_family)
        list_id, list_name = _choose(lists, d.get('list_id'), current)
        _save('', '', list_id, list_name)
        return jsonify(connected=True, via_hub=True, list_id=list_id, list_name=list_name, lists=lists)
    url = _url(d.get('url') or (current['url'] if current else ''))
    token = text(d.get('token'), 200) or (current['token'] if current else '')
    if not token:
        abort(400, 'The Kooka token is required')
    lists = _lists(url, token)
    list_id, list_name = _choose(lists, d.get('list_id'), current)
    _save(url, token, list_id, list_name)
    return jsonify(connected=True, url=url, list_id=list_id, list_name=list_name, lists=lists)


@bp.delete('/integrations/kooka')
def disconnect():
    require_admin()
    with db.transaction() as conn:
        conn.execute('DELETE FROM kooka_links WHERE family_id = ?', (g.family_id,))
    return jsonify(connected=False)


@bp.post('/integrations/kooka/send')
def send():
    """items: names of foods → added to the chosen Kooka list (Kooka skips what is already on it)."""
    names = body().get('items')
    if not isinstance(names, list) or not names or len(names) > 200:
        abort(400, 'Nothing to send')
    names = [text(n, 120) for n in names if isinstance(n, str) and text(n, 120)]
    with db.transaction() as conn:
        link = _link(conn)
        hub_family = _hub_family(conn)
    if not link or not link['list_id'] or not (hub_family or link['token']):
        abort(400, 'Connect Kooka and choose a list first')
    if hub_family:
        r = _via_hub(hub_family, 'POST', f"/lists/{link['list_id']}/items", {'items': names})
    else:
        r = _call(link['url'], link['token'], 'POST', f"/lists/{link['list_id']}/items", {'items': names})
    return jsonify(added=len(r.get('added', [])), skipped=len(r.get('skipped', [])), list_name=link['list_name'])
