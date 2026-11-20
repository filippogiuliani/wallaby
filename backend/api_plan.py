"""API: the pediatrician's weekly plan (per child).

Two parts, both optional:
- frequencies: how many times a week to offer each macro category (minimum and/or maximum), counted at lunch and
  dinner only (main_only, the default for proteins: the yogurt at snack is not one of the "2 fresh cheeses a week") or
  at every meal;
- menu: for each weekday and meal, the planned foods and a note.
"""
import datetime

from flask import Blueprint, abort, g, jsonify, request

import db
import i18n
from common import body, check_family_foods, family_child, id_list, requested_child, text, valid_date

bp = Blueprint('plan', __name__)

MAX_TIMES = 28  # 4 meals × 7 days


def _macros(conn):
    return [r[0] for r in conn.execute('SELECT macro FROM categories GROUP BY macro ORDER BY MIN(sort_order)')]


def default_main_only(conn):
    """{macro: counted only at lunch and dinner by default}: the proteins yes, fruit, vegetables and the rest no."""
    return {r['macro']: r['protein'] == 1 for r in conn.execute(
        "SELECT macro, MAX(diary_group = 'protein') AS protein FROM categories GROUP BY macro")}


def main_only(conn, macro, stored):
    return bool(stored) if stored is not None else default_main_only(conn).get(macro, False)


def read_plan(conn, child_id):
    frequencies = [{'macro': r['macro'], 'min': r['min_times'], 'max': r['max_times'],
                    'main_only': main_only(conn, r['macro'], r['main_only'])} for r in conn.execute(
        'SELECT macro, min_times, max_times, main_only FROM plan_frequencies WHERE child_id = ?', (child_id,))]
    menu = []
    for m in conn.execute('SELECT id, weekday, meal_type, notes FROM plan_meals WHERE child_id = ? ORDER BY weekday', (child_id,)):
        foods = [r[0] for r in conn.execute(
            '''SELECT pmf.food_id FROM plan_meal_foods pmf JOIN foods f ON f.id = pmf.food_id
               JOIN categories c ON c.id = f.category_id WHERE pmf.plan_meal_id = ? ORDER BY c.sort_order, f.name''', (m['id'],))]
        menu.append({'weekday': m['weekday'], 'meal_type': m['meal_type'], 'notes': m['notes'], 'foods': foods})
    return {'frequencies': frequencies, 'menu': menu}


def _times(value):
    if value in (None, ''):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        abort(400, 'Invalid number of times')
    if not 0 <= n <= MAX_TIMES:
        abort(400, 'Invalid number of times')
    return n


def write_plan(conn, child_id, d, check_foods=True):
    """Replace the child's plan. Empty entries (no min/max, no foods and no note) are dropped."""
    macros = set(_macros(conn))
    frequencies = d.get('frequencies') or []
    menu = d.get('menu') or []
    if not isinstance(frequencies, list) or not isinstance(menu, list):
        abort(400, 'Invalid plan')

    conn.execute('DELETE FROM plan_frequencies WHERE child_id = ?', (child_id,))
    conn.execute('DELETE FROM plan_meals WHERE child_id = ?', (child_id,))

    for f in frequencies:
        if not isinstance(f, dict) or f.get('macro') not in macros:
            abort(400, 'Invalid plan')
        low, high = _times(f.get('min')), _times(f.get('max'))
        if low is None and high is None:
            continue
        if low is not None and high is not None and low > high:
            abort(400, 'The minimum cannot exceed the maximum')
        only = f.get('main_only')
        only = None if only is None or bool(only) == default_main_only(conn).get(f['macro'], False) else int(bool(only))
        conn.execute('INSERT OR REPLACE INTO plan_frequencies (child_id, macro, min_times, max_times, main_only) VALUES (?,?,?,?,?)',
                     (child_id, f['macro'], low, high, only))

    for m in menu:
        if not isinstance(m, dict) or m.get('meal_type') not in db.MEAL_TYPES or m.get('weekday') not in range(7):
            abort(400, 'Invalid plan')
        foods = id_list(m.get('foods'), 'foods')
        notes = text(m.get('notes'), 500)
        if not foods and not notes:
            continue
        if check_foods:
            check_family_foods(conn, foods)
        plan_meal_id = conn.execute('INSERT OR REPLACE INTO plan_meals (child_id, weekday, meal_type, notes) VALUES (?,?,?,?)',
                                    (child_id, m['weekday'], m['meal_type'], notes)).lastrowid
        conn.executemany('INSERT OR IGNORE INTO plan_meal_foods VALUES (?,?)', [(plan_meal_id, f) for f in foods])


@bp.get('/plan')
def get_plan():
    with db.transaction() as conn:
        return jsonify(read_plan(conn, requested_child(conn)))


@bp.put('/plan')
def save_plan():
    d = body()
    with db.transaction() as conn:
        child_id = family_child(conn, d.get('child_id'))
        write_plan(conn, child_id, d)
        return jsonify(read_plan(conn, child_id))


@bp.post('/plan/copy')
def copy_plan():
    """Copy one child's plan onto another (replacing the target's plan)."""
    d = body()
    with db.transaction() as conn:
        source = family_child(conn, d.get('from'))
        target = family_child(conn, d.get('to'))
        if source == target:
            abort(400, 'Choose another child')
        write_plan(conn, target, read_plan(conn, source), check_foods=False)
        return jsonify(read_plan(conn, target))


@bp.get('/plan/week')
def week_progress():
    """Progress against the frequencies in the week (Monday–Sunday) containing ?date=.
    Counts the meals (not the single foods) where the macro category appears, refusals excluded; for the frequencies
    counted at lunch and dinner only (main_only), only those meals."""
    date = datetime.date.fromisoformat(valid_date(request.args.get('date') or datetime.date.today().isoformat()))
    monday = date - datetime.timedelta(days=date.weekday())
    sunday = monday + datetime.timedelta(days=6)
    with db.transaction() as conn:
        child_id = requested_child(conn)
        targets = {r['macro']: r for r in conn.execute(
            'SELECT macro, min_times, max_times, main_only FROM plan_frequencies WHERE child_id = ?', (child_id,))}
        main = ','.join(f"'{t}'" for t in db.MAIN_MEALS)
        counts = {}
        for r in conn.execute(
                f'''SELECT c.macro, COUNT(DISTINCT m.id) AS n, COUNT(DISTINCT CASE WHEN m.meal_type IN ({main}) THEN m.id END) AS n_main
                    FROM meals m JOIN meal_foods mf ON mf.meal_id = m.id JOIN foods f ON f.id = mf.food_id
                    JOIN categories c ON c.id = f.category_id
                    WHERE m.child_id = ? AND m.date BETWEEN ? AND ? AND mf.amount != 'refused'
                    GROUP BY c.macro''', (child_id, monday.isoformat(), sunday.isoformat())):
            counts[r['macro']] = (r['n'], r['n_main'])
        rows = []
        for m in _macros(conn):
            if m in targets:
                only = main_only(conn, m, targets[m]['main_only'])
                n, n_main = counts.get(m, (0, 0))
                rows.append({'macro': m, 'times': n_main if only else n, 'min': targets[m]['min_times'],
                             'max': targets[m]['max_times'], 'main_only': only})
    return jsonify(start=monday.isoformat(), end=sunday.isoformat(), frequencies=rows)


@bp.get('/plan/documents')
def plan_documents():
    """The paediatrician's plans of the child archived in Pelican (Wombat mode, for someone who uses Pelican):
    {available, documents: [{id, title, doc_type, doc_date, issuer, excerpt, url}]}."""
    from famkit import hub
    with db.transaction() as conn:
        child_id = requested_child(conn)
        child = conn.execute('SELECT hub_id FROM children WHERE id = ?', (child_id,)).fetchone()
        ids = hub.family_ids(conn, g.family_id) if hub.enabled() else None
    if not ids or not child or not child['hub_id']:
        return jsonify(available=False, documents=[])
    try:
        docs = hub.ask('pelican', 'GET', f"/plans?child={int(child['hub_id'])}", ids[0], ids[1])
    except hub.AskError:
        return jsonify(available=False, documents=[])    # no Pelican for this person (or not connected)
    except hub.Unavailable as e:
        print(f'[pelican] {e}', flush=True)
        return jsonify(available=True, documents=[], error=i18n.tr('Pelican is not reachable'))
    keep = ('id', 'title', 'doc_type', 'doc_date', 'issuer', 'excerpt', 'url')
    return jsonify(available=True, documents=[{k: d.get(k) for k in keep} for d in docs if isinstance(d, dict)])
