#!/usr/bin/env python3
"""Actualiza NFL EDGE desde nflverse sin Apps Script y sin datos inventados.

Fuente del calendario/resultados: nflverse schedules/games.csv
Fuente de EPA/Success Rate: nflverse pbp/play_by_play_2026.csv.gz
Se ejecuta en GitHub Actions. Si la fuente PBP está retrasada, NO publica
un nuevo corte inconsistente. Los snapshots prepartido son inmutables.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import math
import re
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SEASON = 2026
SCHEDULE_URL = 'https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv'
PBP_URL = f'https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{SEASON}.csv.gz'
ALIASES = {'LA': 'LAR', 'JAC': 'JAX', 'WSH': 'WAS', 'OAK': 'LV', 'SD': 'LAC', 'STL': 'LAR'}
MODEL_VERSION = 'v8-heuristica-1'
DEFAULT_WEIGHT = 1.5


def norm(code):
    c = (code or '').strip().upper()
    return ALIASES.get(c, c)


def num(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def download(url: str) -> bytes:
    error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={
                'User-Agent': 'NFL-EDGE-educational/1.0', 'Accept': '*/*'})
            with urllib.request.urlopen(request, timeout=70) as response:
                return response.read()
        except Exception as exc:
            error = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'No se pudo descargar {url}: {error}')


def records_from_csv(data: bytes, gz=False):
    stream = io.BytesIO(data)
    if gz:
        stream = gzip.GzipFile(fileobj=stream)
    with io.TextIOWrapper(stream, encoding='utf-8-sig', errors='replace', newline='') as f:
        yield from csv.DictReader(f)


def fixture_ids():
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    match = re.search(r'const WEEKS=(\{.*?\});\s*(?:const|let) ', html, re.S)
    if not match:
        raise RuntimeError('No se encuentra const WEEKS en index.html')
    original = json.loads(match.group(1))
    ids = {}
    for key, block in original.items():
        week = int(key)
        for i, label in enumerate(block['raw']):
            away, home = (norm(k) for k in label.split('@'))
            ident = (week, away, home)
            if ident in ids:
                raise RuntimeError(f'Fixture duplicado en HTML: {ident}')
            ids[ident] = f'w{week}-{i}'
    if len(ids) != 272:
        raise RuntimeError(f'Se esperaban 272 partidos en el HTML, hay {len(ids)}')
    return ids


def load_schedule(data):
    valid = []
    seen = set()
    for row in records_from_csv(data):
        if str(row.get('season')) not in ('2025', '2026') or row.get('game_type') != 'REG':
            continue
        week = int(row['week'])
        if not 1 <= week <= 18:
            continue
        away, home = norm(row['away_team']), norm(row['home_team'])
        ident = (int(row['season']), week, away, home)
        if ident in seen:
            raise RuntimeError(f'Partido duplicado en calendario oficial {ident}')
        seen.add(ident)
        valid.append({
            'season': int(row['season']), 'week': week, 'away': away, 'home': home,
            'game_id': row.get('game_id', ''), 'gameday': row.get('gameday', ''),
            'gametime': row.get('gametime', ''), 'away_score': num(row.get('away_score')),
            'home_score': num(row.get('home_score')),
        })
    return valid


def completed_cutoff(schedule):
    weeks = defaultdict(list)
    for game in schedule:
        if game['season'] == SEASON:
            weeks[game['week']].append(game)
    if len(weeks) != 18 or sum(map(len, weeks.values())) != 272:
        raise RuntimeError('El calendario 2026 descargado no contiene 18 semanas y 272 encuentros')
    cutoff = 0
    for week in range(1, 19):
        games = weeks[week]
        if games and all(g['away_score'] is not None and g['home_score'] is not None for g in games):
            cutoff = week
        else:
            break
    if cutoff == 0:
        raise RuntimeError('Todavía no se ha completado la Semana 1')
    return cutoff


def rec_template():
    return {'home': [0, 0, 0], 'away': [0, 0, 0], 'pf': 0, 'pa': 0}


def make_records(schedule, cutoff, codes):
    stats = {code: {'2025': rec_template(), '2026': rec_template()} for code in codes}
    for g in schedule:
        year = g['season']
        if year == 2026 and g['week'] > cutoff:
            continue
        a, h = g['away'], g['home']
        if a not in stats or h not in stats:
            raise RuntimeError(f'Equipo no identificado: {a} {h}')
        if g['away_score'] is None or g['home_score'] is None:
            if year == 2025:
                raise RuntimeError(f'Resultado faltante de 2025: {a} @ {h}')
            continue
        aw, hw = g['away_score'], g['home_score']
        ix_a, ix_h = (0, 1) if aw > hw else ((1, 0) if aw < hw else (2, 2))
        stats[a][str(year)]['away'][ix_a] += 1
        stats[h][str(year)]['home'][ix_h] += 1
        stats[a][str(year)]['pf'] += int(aw)
        stats[a][str(year)]['pa'] += int(hw)
        stats[h][str(year)]['pf'] += int(hw)
        stats[h][str(year)]['pa'] += int(aw)
    if any(sum(sum(t['2025'][s]) for s in ('home', 'away')) != 17 for t in stats.values()):
        raise RuntimeError('El calendario 2025 no tiene 17 partidos completos para todos los equipos')
    return stats


def pbp_efficiency(raw, cutoff, game_ids, codes):
    counters = {code: {'off': [0.0, 0, 0.0, 0], 'def': [0.0, 0, 0.0, 0]} for code in codes}
    found = set()
    valid_game_ids = set(game_ids)
    for row in records_from_csv(raw, gz=True):
        if row.get('season_type') != 'REG':
            continue
        try:
            week = int(row.get('week') or 0)
        except ValueError:
            continue
        gid = row.get('game_id', '')
        if week > cutoff or gid not in valid_game_ids:
            continue
        found.add(gid)
        if row.get('play_type') not in ('pass', 'run'):
            continue
        a, d = norm(row.get('posteam')), norm(row.get('defteam'))
        epa, success = num(row.get('epa')), num(row.get('success'))
        if a not in counters or d not in counters or epa is None:
            continue
        for team, side in ((a, 'off'), (d, 'def')):
            arr = counters[team][side]
            arr[0] += epa
            arr[1] += 1
            if success is not None:
                arr[2] += success
                arr[3] += 1
    missing = valid_game_ids - found
    if missing:
        sample = ', '.join(sorted(missing)[:5])
        raise RuntimeError(f'El EPA todavía no está disponible para {len(missing)} partidos completados, por ejemplo {sample}. Se conserva el corte anterior.')
    result = {}
    for code, data in counters.items():
        off, defense = data['off'], data['def']
        if min(off[1], defense[1], off[3], defense[3]) == 0:
            raise RuntimeError(f'Falta EPA/success para {code}')
        result[code] = [round(off[0] / off[1], 5), round(defense[0] / defense[1], 5),
                        round(off[2] / off[3] * 100, 2), round(defense[2] / defense[3] * 100, 2)]
    return result


def winrate(rec):
    n = sum(rec)
    return (rec[0] + .5 * rec[2]) / n if n else .5


def venue(team, side, weight=DEFAULT_WEIGHT):
    old, cur = team['2025'][side], team['2026'][side]
    return (old[0] + old[2] * .5 + weight * (cur[0] + cur[2] * .5) + 2) / (sum(old) + weight * sum(cur) + 4)


def overall(team):
    def joined(y):
        return [a + b for a, b in zip(team[y]['home'], team[y]['away'])]
    return .72 * winrate(joined('2026')) + .28 * winrate(joined('2025'))


def predict(g, teams):
    a, h = g['away'], g['home']
    ta, th = teams[a], teams[h]
    def net(t): return t['eff'][0] - t['eff'][1]
    def margin(t):
        r = t['2026']
        n = sum(r['home']) + sum(r['away'])
        return (r['pf'] - r['pa']) / n if n else 0
    neutral = g.get('neutral', False)
    parts = [
        ('EPA neto (eficiencia)', 2.65 * (net(th) - net(ta))),
        ('Balance general 2025–26', .85 * (overall(th) - overall(ta))),
        ('Diferencia de puntos / juego', .014 * (margin(th) - margin(ta))),
        ('Local / visitante', 0 if neutral else .52 * (venue(th, 'home') - venue(ta, 'away'))),
        ('Ventaja de sede base', 0 if neutral else .11),
    ]
    z = sum(v for _, v in parts)
    p = 1 / (1 + math.exp(-z))
    winner = h if p >= .5 else a
    return {
        'p': round(p, 7), 'winning': winner, 'losing': a if winner == h else h,
        'edge': round(max(p, 1-p) * 100),
        'components': [{'name': k, 'v': round(v, 7)} for k, v in parts],
    }


def fixtures_for_week(week, source, ids):
    fixtures = []
    for g in source:
        if g['season'] != SEASON or g['week'] != week:
            continue
        ident = (week, g['away'], g['home'])
        if ident not in ids:
            raise RuntimeError(f'Fixture distinto entre NFL EDGE y el calendario: {ident}')
        x = dict(g)
        x['id'] = ids[ident]
        x['neutral'] = False  # Sede neutral se especifica manualmente en la interfaz para Semana 6.
        fixtures.append(x)
    if len(fixtures) != sum(k[0] == week for k in ids):
        raise RuntimeError(f'No coinciden los partidos de Semana {week} con el HTML')
    return fixtures


def first_kickoff(fixtures):
    eastern = ZoneInfo('America/New_York')
    times = []
    for g in fixtures:
        day = g['gameday']
        kickoff = g['gametime']
        if not day or not kickoff:
            return None  # No congelar un pronóstico si no existe fecha/hora confiable.
        try:
            timepart = kickoff[:5]
            moment = datetime.fromisoformat(f'{day}T{timepart}:00').replace(tzinfo=eastern)
        except ValueError:
            return None
        times.append(moment.astimezone(timezone.utc))
    return min(times) if times else None


def write_if_changed(path, value, overwrite=True):
    if path.exists() and not overwrite:
        return False
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    if path.exists() and path.read_text(encoding='utf-8') == serialized:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding='utf-8')
    return True


def run(schedule_bytes, pbp_bytes):
    ids = fixture_ids()
    codes = set(x for (_, a, h) in ids for x in (a, h))
    schedule = load_schedule(schedule_bytes)
    scheduled_cutoff = completed_cutoff(schedule)
    # Una jornada puede haber terminado pero NFLverse PBP aún estar retrasado.
    # Elegimos el último corte CONTIGUO que tenga resultados Y EPA, sin inventar.
    pbp_ids = {row.get('game_id', '') for row in records_from_csv(pbp_bytes, gz=True)
               if row.get('season_type') == 'REG'}
    weekly_ids = defaultdict(set)
    for g in schedule:
        if g['season'] == SEASON:
            weekly_ids[g['week']].add(g['game_id'])
    cutoff = 0
    for week in range(1, scheduled_cutoff + 1):
        if weekly_ids[week] and weekly_ids[week].issubset(pbp_ids):
            cutoff = week
        else:
            break
    if cutoff == 0:
        raise RuntimeError('La Semana 1 aún no tiene EPA suficiente')
    if cutoff < scheduled_cutoff:
        print(f'AVISO: resultados hasta Semana {scheduled_cutoff}, EPA confirmado solo hasta Semana {cutoff}.')
    # Si el calendario cambia, detenerse antes de publicar datos que no correspondan al HTML.
    all_2026 = {(g['week'], g['away'], g['home']) for g in schedule if g['season'] == SEASON}
    if all_2026 != set(ids):
        missing = sorted(set(ids) - all_2026)[:4]
        extra = sorted(all_2026 - set(ids))[:4]
        raise RuntimeError(f'Calendario distinto al HTML (faltan={missing}, sobran={extra}). No se publicó el corte.')
    standings = make_records(schedule, cutoff, codes)
    finished = [g for g in schedule if g['season'] == SEASON and g['week'] <= cutoff]
    if len(finished) != sum(1 for (week, _, _) in ids if week <= cutoff):
        raise RuntimeError('No coinciden partidos completos con calendario local')
    eff = pbp_efficiency(pbp_bytes, cutoff, [g['game_id'] for g in finished], codes)
    teams = {code: {**standings[code], 'eff': eff[code]} for code in sorted(codes)}
    previous_path = DATA / 'actual.json'
    prev = json.loads(previous_path.read_text(encoding='utf-8')) if previous_path.exists() else {}
    if prev.get('through_week', 0) > cutoff:
        raise RuntimeError('La nueva fuente tiene menos jornadas completas que el último corte. No se sobrescribe.')
    next_week = cutoff + 1 if cutoff < 18 else None
    now = datetime.now(timezone.utc)
    fresh = {
        'schema': 1, 'season': SEASON, 'through_week': cutoff, 'next_week': next_week,
        'updated_at_utc': now.isoformat(timespec='seconds'),
        'model_version': MODEL_VERSION, 'weight_for_snapshots': DEFAULT_WEIGHT,
        'sources': {'schedule': SCHEDULE_URL, 'pbp': PBP_URL}, 'teams': teams,
        'results': {}, 'snapshots': {},
    }
    for g in finished:
        away, home = g['away_score'], g['home_score']
        key = ids[(g['week'], g['away'], g['home'])]
        fresh['results'][key] = {'away': int(away), 'home': int(home),
                                 'winner': g['away'] if away > home else (g['home'] if home > away else 'TIE'),
                                 'week': g['week']}
    # Se mantienen snapshots anteriores; nunca se reescriben por cambios de estadísticas.
    for file in sorted((DATA / 'snapshots').glob('semana-*.json')):
        snapshot = json.loads(file.read_text(encoding='utf-8'))
        wk = str(snapshot.get('week'))
        if snapshot.get('season') != SEASON or not isinstance(snapshot.get('games'), dict):
            raise RuntimeError(f'Snapshot corrupto: {file}')
        fresh['snapshots'][wk] = snapshot['games']
    created = False
    if next_week is not None:
        path = DATA / 'snapshots' / f'semana-{next_week:02d}.json'
        if not path.exists():
            upcoming = fixtures_for_week(next_week, schedule, ids)
            kickoff = first_kickoff(upcoming)
            if kickoff and now < kickoff:
                snap = {'schema': 1, 'season': SEASON, 'week': next_week,
                        'created_at_utc': now.isoformat(timespec='seconds'),
                        'data_through_week': cutoff, 'model_version': MODEL_VERSION,
                        'games': {g['id']: predict(g, teams) for g in upcoming}}
                created = write_if_changed(path, snap, overwrite=False)
                fresh['snapshots'][str(next_week)] = snap['games']
            else:
                print('AVISO: sin snapshot nuevo: el kickoff ya ocurrió o falta horario oficial.')
    # No republicar datos idénticos cuando no hubo un nuevo corte ni correcciones.
    def essential(d):
        return {'through_week': d.get('through_week'), 'teams': d.get('teams'),
                'results': d.get('results'), 'snapshots': d.get('snapshots')}
    if essential(prev) == essential(fresh):
        print(f'SIN CAMBIOS: corte hasta Semana {cutoff}; no hay datos nuevos de EPA.')
        return
    write_if_changed(previous_path, fresh)
    print(f'OK: estadísticas hasta Semana {cutoff}, {len(fresh["results"])} resultados, {len(fresh["snapshots"])} snapshots.' + (' Se congeló el siguiente pronóstico.' if created else ''))


def main():
    try:
        schedule = download(SCHEDULE_URL)
        pbp = download(PBP_URL)
        run(schedule, pbp)
    except Exception as error:
        # No se despliegan datos parciales. Si ya existe un corte válido, no se pierde.
        if (DATA / 'actual.json').exists():
            print(f'AVISO: no se publicó un corte nuevo: {error}', file=sys.stderr)
            sys.exit(0)
        print(f'ERROR INICIAL: {error}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
