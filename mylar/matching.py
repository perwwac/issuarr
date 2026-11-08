"""Matching the files in a series folder to its issues: matches made by hand on the series page,
and files whose ComicInfo tags carry a ComicVine issue ID of the series."""
import os
import re
import shutil
import threading
import time

import mylar
from mylar import comicinfo, db, logger

_TAGS = {}   # (path, size, mtime) -> ComicVine issue ID, so a rescan doesn't reopen unchanged files
_LOCK = threading.Lock()


def _extensions():
    return tuple(x for x in ('.cbr', '.cbz', '.cb7', '.pdf') if x not in (mylar.CONFIG.IGNORE_SEARCH_WORDS or []))


YEAR_GAP = 3   # years between a file's name and its issue's cover date before it counts as another volume's


def name_year(name):
    """The first four-digit year in brackets in a file name, or None."""
    m = re.search(r'[(\[]\s*(?:[^)\]]*?[\s,\-/])?((?:19|20)\d\d)(?=[\s,\-/)\]])', name or '')
    return int(m.group(1)) if m else None


def year_off(name, issue_year):
    """Whether the year in a file name is YEAR_GAP or more years from the issue's. A file with no year, or an issue
    with none, is never off."""
    fy = name_year(name)
    try:
        iy = int(str(issue_year)[:4])
    except (TypeError, ValueError):
        return False
    return fy is not None and abs(fy - iy) >= YEAR_GAP


def year_preference(new, existing, issue_year):
    """Of two files for one issue, 'new' or 'existing' when only that one's year fits the issue (the other is
    YEAR_GAP or more years away), else None and the size or type rule decides."""
    new_off, old_off = year_off(new, issue_year), year_off(existing, issue_year)
    if old_off and not new_off:
        return 'new'
    if new_off and not old_off:
        return 'existing'
    return None


def series_dirs(comic):
    """The series folder, and its copy under the second library folder when there is one."""
    if not comic or not comic['ComicLocation']:
        return []
    dirs = [comic['ComicLocation']]
    extra = mylar.CONFIG.MULTIPLE_DEST_DIRS
    if extra and extra != 'None':
        try:
            alt = os.path.join(extra, os.path.basename(comic['ComicLocation']))
            if not os.path.exists(alt):
                alt = mylar.filers.FileHandlers(ComicID=comic['ComicID']).secondary_folders(comic['ComicLocation'])
            if alt and alt not in dirs:
                dirs.append(alt)
        except Exception as e:
            logger.fdebug('[MATCHING] No second folder for %s: %s' % (comic['ComicLocation'], e))
    return dirs


def walk(dirs):
    """Every comic file in the folders, the way a rescan sees them."""
    ext = _extensions()
    out = []
    for top in dirs:
        if not os.path.isdir(top):
            continue
        for here, subs, names in os.walk(top):
            if '.AppleDouble' in here:
                continue
            for name in names:
                if name.startswith('.') or not name.lower().endswith(ext):
                    continue
                path = os.path.join(here, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size:
                    sub = os.path.relpath(here, top)
                    out.append({'path': path, 'name': name, 'sub': None if sub == '.' else sub, 'size': size})
    return out


def pins(comicid, dirs):
    """The files matched by hand, by name, each with its issue (or annual). A match whose file is gone
    is dropped, so the issue is matched by name again."""
    myDB = db.DBConnection()
    rows = [('issues', r) for r in myDB.select('SELECT IssueID, Location FROM issues WHERE ComicID=? AND forced_file=1', [comicid])]
    if mylar.CONFIG.ANNUALS_ON:
        rows += [('annuals', r) for r in myDB.select(
            'SELECT IssueID, Location FROM annuals WHERE ComicID=? AND forced_file=1 AND NOT Deleted', [comicid])]
    if not rows:
        return {}
    found = {}
    names = None
    for table, r in rows:
        name = r['Location']
        here = name and any(os.path.isfile(os.path.join(d, name)) for d in dirs)
        if name and not here:
            if names is None:
                names = {f['name'] for f in walk(dirs)}
            here = name in names
        if here:
            found[name] = r['IssueID']
        else:
            logger.info('[MATCHING] The file matched by hand to issue %s is gone - matching it by name again.' % r['IssueID'])
            myDB.action('UPDATE %s SET forced_file=0 WHERE IssueID=?' % table, [r['IssueID']])
    return found


def tagged_id(path, size=None):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_size, st.st_mtime)
    with _LOCK:
        if key in _TAGS:
            return _TAGS[key]
    cvid = comicinfo.cv_issue_id(path)
    with _LOCK:
        if len(_TAGS) > 20000:
            _TAGS.clear()
        _TAGS[key] = cvid
    return cvid


def by_tags(comicid, files, pinned, taken):
    """Of files the name didn't place, those whose tags name an issue (or annual) of this series
    that no other file has. files: (folder, subfolder, name, size)."""
    myDB = db.DBConnection()
    taken = set(str(t) for t in taken) | set(str(i) for i in pinned.values())
    seen = set()
    out = []
    for folder, sub, name, size in files:
        if not name or name in pinned:
            continue
        path = os.path.join(folder, sub or '', name)
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        cvid = tagged_id(path)
        if not cvid or cvid in taken:
            continue
        row = myDB.selectone('SELECT Issue_Number FROM issues WHERE ComicID=? AND IssueID=?', [comicid, cvid]).fetchone()
        annual = False
        if row is None and mylar.CONFIG.ANNUALS_ON:
            row = myDB.selectone('SELECT Issue_Number FROM annuals WHERE ComicID=? AND IssueID=? AND NOT Deleted', [comicid, cvid]).fetchone()
            annual = row is not None
        if row is None:
            continue
        taken.add(cvid)
        if size is None:
            size = os.path.getsize(path)
        out.append({'issueid': cvid, 'number': row['Issue_Number'], 'name': name, 'size': str(size), 'annual': annual})
    return out


def _comic(comicid):
    return db.DBConnection().selectone('SELECT ComicID, ComicName, ComicYear, ComicLocation FROM comics WHERE ComicID=?', [comicid]).fetchone()


def _issue_choice(i, detail=False, annual=False):
    """Slim issue fields for the Match dialog select (id + label + whether a cover exists).
    detail=True adds the fields Match-a-file needs for one focused issue."""
    out = {'issueid': i['IssueID'], 'number': i['Issue_Number'], 'name': i['IssueName'],
           'cover': bool(i.get('ImageURL') or i.get('ImageURL_ALT')), 'annual': bool(annual)}
    if detail:
        out['file'] = i['Location'] if i['Status'] == 'Downloaded' else None
        out['pinned'] = bool(i.get('forced_file'))
    return out


def _has_file(i):
    """Archived issues keep the name of a file that has gone, so only Downloaded ones count as matched."""
    return bool(i['Location'] and i['Status'] == 'Downloaded')


def _in_dropdown(i):
    """Match select: any issue/annual that does not already have a Downloaded file."""
    return not _has_file(i)


def files(comicid, issueid=None):
    """The series' files, each with the issue it counts as now and the issue number its name reads as,
    and the unmapped issues/annuals to choose from in Match (already-matched ones are omitted so the
    browser never builds thousands of useless <option>s). Skipped issues/annuals stay in the list so
    a hand match can still claim them. Pass issueid when opening Match a file so that one issue is
    included even if it already has a file."""
    from mylar import attention
    comic = _comic(comicid)
    if comic is None:
        return {'ok': False, 'note': 'That series isn\'t on your watchlist.'}
    myDB = db.DBConnection()
    issues = [dict(r) for r in myDB.select(
        'SELECT IssueID, Issue_Number, IssueName, Status, Location, forced_file, ImageURL, ImageURL_ALT, Int_IssueNumber FROM issues '
        'WHERE ComicID=? ORDER BY Int_IssueNumber', [comicid])]
    annuals = []
    if mylar.CONFIG.ANNUALS_ON:
        annuals = [dict(r) for r in myDB.select(
            'SELECT IssueID, Issue_Number, IssueName, Status, Location, forced_file, Int_IssueNumber FROM annuals '
            'WHERE ComicID=? AND NOT Deleted ORDER BY Int_IssueNumber', [comicid])]
    linked = {}
    for i in issues:
        if _has_file(i):
            linked[i['Location']] = dict(i, annual=False)
    for i in annuals:
        if _has_file(i):
            linked[i['Location']] = dict(i, annual=True)
    out = []
    for f in walk(series_dirs(comic)):
        i = linked.get(f['name'])
        out.append({'name': f['name'], 'sub': f['sub'], 'size': f['size'],
                    'issueid': i['IssueID'] if i else None, 'number': i['Issue_Number'] if i else None,
                    'issue_name': i['IssueName'] if i else None,
                    'cover': bool(i.get('ImageURL') or i.get('ImageURL_ALT')) if i else False,
                    'pinned': bool(i and i.get('forced_file')), 'annual': bool(i and i.get('annual')),
                    'reads_as': attention.parse(f['path']).get('issue')})
    out.sort(key=lambda f: (f['issueid'] is not None, (f['sub'] or '').lower(), f['name'].lower()))
    # Select options: issues/annuals with no file yet (plus the focused issue for Match-a-file).
    choices = [_issue_choice(i) for i in issues if _in_dropdown(i)]
    choices += [_issue_choice(i, annual=True) for i in annuals if _in_dropdown(i)]
    if issueid:
        focused = next((i for i in issues if str(i['IssueID']) == str(issueid)), None)
        annual = False
        if focused is None:
            focused = next((i for i in annuals if str(i['IssueID']) == str(issueid)), None)
            annual = focused is not None
        if focused is not None:
            detail = _issue_choice(focused, detail=True, annual=annual)
            choices = [detail if str(c['issueid']) == str(issueid) else c for c in choices]
            if not any(str(c['issueid']) == str(issueid) for c in choices):
                choices = [detail] + choices
    return {'ok': True, 'folder': comic['ComicLocation'], 'files': out, 'waiting': waiting(comicid),
            'issues': choices}


def waiting(comicid):
    """Files in Needs attention that look like this series, each with the issue its name points to."""
    from mylar import attention
    out = []
    try:
        listed = attention.items()
    except Exception as e:
        logger.fdebug('[MATCHING] Could not read Needs attention: %s' % e)
        return out
    for i in listed:
        if i['in_library'] or (i.get('job') or {}).get('state') in ('waiting', 'filing'):
            continue
        m = next((m for m in i['matches'] if str(m['comicid']) == str(comicid)), None)
        if m is None:
            continue
        out.append({'path': i['path'], 'name': i['name'], 'size': i['size'], 'modified': round(i['modified']),
                    'reads_as': (i['parsed'] or {}).get('issue'), 'issueid': m.get('issueid'), 'number': m.get('number')})
    return out


def cover(comicid, name):
    """The front cover of a file in the series folder (see attention.cover_of), or None."""
    from mylar import attention
    comic = _comic(comicid)
    f = next((f for f in walk(series_dirs(comic)) if f['name'] == name), None) if comic else None
    return attention.cover_of(f['path']) if f else None


def _rescan(comicid):
    from mylar import updater
    updater.forceRescan(comicid, module='[MATCHING]')


def _recount_have(comicid):
    """Have count from the DB only — used after a hand match so we skip a full folder rescan."""
    from mylar import updater
    myDB = db.DBConnection()
    comic = myDB.selectone('SELECT Total FROM comics WHERE ComicID=?', [comicid]).fetchone()
    if comic is None:
        return
    have = myDB.selectone(
        "SELECT COUNT(*) AS n FROM issues WHERE ComicID=? AND Status IN ('Downloaded','Archived')", [comicid]
    ).fetchone()['n']
    if mylar.CONFIG.ANNUALS_ON:
        have += myDB.selectone(
            "SELECT COUNT(*) AS n FROM annuals WHERE ComicID=? AND NOT Deleted AND Status IN ('Downloaded','Archived')",
            [comicid],
        ).fetchone()['n']
    if mylar.CONFIG.IGNORE_HAVETOTAL:
        have += myDB.selectone("SELECT COUNT(*) AS n FROM issues WHERE ComicID=? AND Status='Ignored'", [comicid]).fetchone()['n']
        if mylar.CONFIG.ANNUALS_ON:
            have += myDB.selectone(
                "SELECT COUNT(*) AS n FROM annuals WHERE ComicID=? AND NOT Deleted AND Status='Ignored'", [comicid]
            ).fetchone()['n']
    if mylar.CONFIG.SNATCHED_HAVETOTAL:
        have += myDB.selectone("SELECT COUNT(*) AS n FROM issues WHERE ComicID=? AND Status='Snatched'", [comicid]).fetchone()['n']
    updater.totals(comicid, havefiles=int(have), totalfiles=int(comic['Total']), module='[MATCHING]')


def _free_location(myDB, comicid, name, keep_issueid):
    """Any other issue/annual that pointed at this filename no longer has a file."""
    for table in ('issues', 'annuals'):
        deleted = ' AND NOT Deleted' if table == 'annuals' else ''
        myDB.action(
            "UPDATE %s SET forced_file=0, Location=NULL, ComicSize=NULL, Status='Skipped' "
            "WHERE ComicID=? AND Location=? AND IssueID!=? AND Status='Downloaded'%s" % (table, deleted),
            [comicid, name, keep_issueid],
        )
        myDB.action(
            'UPDATE %s SET forced_file=0 WHERE ComicID=? AND Location=? AND IssueID!=?%s' % (table, deleted),
            [comicid, name, keep_issueid],
        )


def match(comicid, issueid, name, recount=True):
    """Make a file in the series folder count as this issue (or annual), whatever its name says. The
    match holds through later rechecks until it is undone or the file goes. No full series rescan:
    pin the file, free any other issue that claimed the same name, refresh Have from the DB.
    recount=False skips the Have update (batch match recounts once at the end)."""
    comic = _comic(comicid)
    if comic is None:
        return {'ok': False, 'note': 'That series isn\'t on your watchlist.'}
    myDB = db.DBConnection()
    table = 'issues'
    issue = myDB.selectone('SELECT IssueID, Issue_Number, Status FROM issues WHERE IssueID=? AND ComicID=?', [issueid, comicid]).fetchone()
    if issue is None and mylar.CONFIG.ANNUALS_ON:
        issue = myDB.selectone(
            'SELECT IssueID, Issue_Number, Status FROM annuals WHERE IssueID=? AND ComicID=? AND NOT Deleted',
            [issueid, comicid],
        ).fetchone()
        table = 'annuals'
    if issue is None:
        return {'ok': False, 'note': 'That issue isn\'t part of this series.'}
    f = next((f for f in walk(series_dirs(comic)) if f['name'] == name), None)
    if f is None:
        return {'ok': False, 'note': 'That file isn\'t in the series folder any more.'}
    _free_location(myDB, comicid, name, issueid)
    myDB.action(
        "UPDATE %s SET forced_file=1, Location=?, ComicSize=?, Status='Downloaded' WHERE IssueID=?" % table,
        [name, str(f['size']), issueid],
    )
    kind = 'annual ' if table == 'annuals' else ''
    logger.info('[MATCHING] %s is now %s#%s of %s, matched by hand.' % (name, kind, issue['Issue_Number'], comic['ComicName']))
    if recount:
        _recount_have(comicid)
    if issue['Status'] not in ('Downloaded', 'Archived'):
        from mylar import notifiers
        notifiers.files_added(comic['ComicName'], comic['ComicYear'], [issue['Issue_Number']], issueid=issueid, module='[MATCHING]',
                              comicid=comicid, path=f['path'])
    return {'ok': True, 'note': '%s is now %s#%s.' % (name, kind, issue['Issue_Number'])}


def match_batch(comicid, items):
    """Match several files at once (Match files dialog). items: [{issueid, name?|path?}].
    Rejects duplicate issueids. No forceRescan; Have is recounted once at the end."""
    from mylar import unmatched
    if not items:
        return {'ok': False, 'matched': 0, 'skipped': 0, 'note': 'Nothing to match.'}
    cleaned = []
    for it in items:
        issueid = str((it or {}).get('issueid') or '').strip()
        name = (it or {}).get('name')
        path = (it or {}).get('path')
        if not issueid or not (name or path):
            continue
        cleaned.append({'issueid': issueid, 'name': name, 'path': path})
    if not cleaned:
        return {'ok': False, 'matched': 0, 'skipped': 0, 'note': 'Nothing to match.'}
    ids = [c['issueid'] for c in cleaned]
    if len(ids) != len(set(ids)):
        return {'ok': False, 'matched': 0, 'skipped': 0,
                'note': 'Two files point at the same issue. Fix the red rows first.'}
    matched, failed = 0, []
    for c in cleaned:
        if c['path']:
            res = unmatched.match_file(c['path'], comicid, c['issueid'], recount=False)
        else:
            res = match(comicid, c['issueid'], c['name'], recount=False)
        if res.get('ok'):
            matched += 1
        else:
            failed.append(res.get('note') or c.get('name') or c.get('path') or 'Failed')
    if matched:
        _recount_have(comicid)
    if matched and not failed:
        note = 'Matched %s file%s.' % (matched, '' if matched == 1 else 's')
    elif matched:
        note = 'Matched %s. %s couldn\'t be matched.' % (matched, len(failed))
    else:
        note = failed[0] if len(failed) == 1 else 'None of those could be matched.'
    return {'ok': matched > 0, 'matched': matched, 'skipped': len(failed), 'note': note, 'failed': failed}


def unmatch(comicid, issueid):
    """Undo a match made by hand: the file is matched by its name again."""
    myDB = db.DBConnection()
    table = 'issues'
    issue = myDB.selectone('SELECT Issue_Number, forced_file FROM issues WHERE IssueID=? AND ComicID=?', [issueid, comicid]).fetchone()
    if issue is None and mylar.CONFIG.ANNUALS_ON:
        issue = myDB.selectone(
            'SELECT Issue_Number, forced_file FROM annuals WHERE IssueID=? AND ComicID=? AND NOT Deleted',
            [issueid, comicid],
        ).fetchone()
        table = 'annuals'
    if issue is None:
        return {'ok': False, 'note': 'That issue isn\'t part of this series.'}
    if issue['forced_file']:
        myDB.action('UPDATE %s SET forced_file=0 WHERE IssueID=?' % table, [issueid])
        logger.info('[MATCHING] Undid the match made by hand for issue %s.' % issueid)
        _rescan(comicid)
    return {'ok': True, 'note': '#%s is matched by file name again.' % issue['Issue_Number']}


def move_to(comicid, newid):
    """Wrong series: move the files of a series added as the wrong ComicVine series into the right
    one's folder, then recheck both so the files match the right series' issues. The wrong series
    stays on the watchlist until it is removed."""
    from mylar import attention
    old, new = _comic(comicid), _comic(newid)
    if old is None:
        return {'ok': False, 'note': 'That series isn\'t on your watchlist.'}
    if new is None or str(newid) == str(comicid):
        return {'ok': False, 'note': 'Pick a different series to move the files to.'}
    if attention.series_state(newid)['state'] != 'ready':
        return {'ok': False, 'note': '%s is still being added. Try again when its issues have loaded.' % new['ComicName']}
    if not new['ComicLocation']:
        return {'ok': False, 'note': '%s has no series folder.' % new['ComicName']}
    moved, kept = 0, []
    for top in series_dirs(old):
        if os.path.normpath(top) == os.path.normpath(new['ComicLocation']):
            continue
        for f in walk([top]):
            if f['path'].startswith(os.path.join(new['ComicLocation'], '')):
                continue   # already in the right series' folder (it sits inside this one)
            dest_dir = os.path.join(new['ComicLocation'], f['sub']) if f['sub'] else new['ComicLocation']
            dest = os.path.join(dest_dir, f['name'])
            if os.path.exists(dest):
                kept.append(f['name'])
                continue
            try:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.move(f['path'], dest)
                moved += 1
            except OSError as e:
                logger.warn('[MATCHING] Could not move %s to %s: %s' % (f['path'], dest_dir, e))
                kept.append(f['name'])
    logger.info('[MATCHING] Moved %s file(s) from %s [%s] to %s [%s].' % (moved, old['ComicName'], comicid, new['ComicName'], newid))
    _rescan(comicid)
    _rescan(newid)
    have = db.DBConnection().selectone("SELECT COUNT(*) AS n FROM issues WHERE ComicID=? AND Status='Downloaded'", [newid]).fetchone()['n']
    label = '%s (%s)' % (new['ComicName'], new['ComicYear']) if new['ComicYear'] else new['ComicName']
    note = 'Moved %s file%s to %s. %s of its issues have a file now.' % (moved, '' if moved == 1 else 's', label, have)
    if kept:
        note += ' %s file%s stayed where %s, because a file with the same name is already there or it couldn\'t be moved.' % (
            len(kept), '' if len(kept) == 1 else 's', 'it was' if len(kept) == 1 else 'they were')
    return {'ok': True, 'moved': moved, 'kept': kept, 'have': have, 'note': note,
            'series': {'comicid': str(newid), 'name': new['ComicName']}}


# Wrong series runs in the background: the page asks whether to move the files and remove the wrong
# series up front, then adding the right one (which can take a minute while its issues load from
# ComicVine), moving, rechecking and removing go on after the dialog is closed.
_JOBS = {}      # wrong series' ComicID -> how its job is going
JOB_POLL = 3    # seconds between looks at whether the right series has loaded
JOB_WAIT = 15 * 60


def wrong_series(comicid, newid, name=None, year=None, move=True, remove=False):
    """Start a Wrong series job: add the right series, then move the files of the wrong one to it
    (move) and take the wrong one off the watchlist (remove). Its folder is never deleted."""
    comicid, newid = str(comicid), re.sub(r'^4050-', '', str(newid or '')).strip()
    old = _comic(comicid)
    if old is None:
        return {'ok': False, 'note': 'That series isn\'t on your watchlist.'}
    if not newid.isdigit() or newid == comicid:
        return {'ok': False, 'note': 'Pick a different series to move the files to.'}
    with _LOCK:
        job = _JOBS.get(comicid)
        if job and job['state'] not in ('done', 'failed'):
            return dict(job, ok=False, note='%s is already being moved to %s.' % (job['old'], job['new']))
        label = lambda n, y: '%s (%s)' % (n, y) if y else n
        job = {'comicid': comicid, 'newid': newid, 'move': bool(move), 'remove': bool(remove),
               'old': label(old['ComicName'], old['ComicYear']), 'new': label(name or 'the right series', year),
               'state': 'adding', 'note': 'Adding %s and loading its issues from ComicVine…' % label(name or 'the right series', year),
               'removed': False}
        _JOBS[comicid] = job
    threading.Thread(target=_wrong_series_run, args=(job, name, year), name='WRONG-SERIES', daemon=True).start()
    return dict(job, ok=True)


def wrong_series_state(comicid):
    job = _JOBS.get(str(comicid))
    return dict(job, ok=True) if job else {'ok': True, 'state': 'none'}


def _wrong_series_run(job, name, year):
    from mylar import attention, helpers
    comicid, newid = job['comicid'], job['newid']

    def step(state, note, **more):
        job.update(more, state=state, note=note)
        logger.info('[MATCHING] Wrong series %s -> %s: %s' % (comicid, newid, note))

    try:
        added = attention.add_series(newid, name, year)
        if not added.get('ok'):
            return step('failed', added.get('note') or 'Couldn\'t add the right series.')
        waited = 0
        while attention.series_state(newid)['state'] != 'ready':
            if waited >= JOB_WAIT:
                return step('failed', '%s is still being added. Open Wrong series again when it shows up in your library.' % job['new'])
            time.sleep(JOB_POLL)
            waited += JOB_POLL
        new = _comic(newid)
        if new is not None:
            job['new'] = '%s (%s)' % (new['ComicName'], new['ComicYear']) if new['ComicYear'] else new['ComicName']
        note = '%s is on your watchlist.' % job['new']
        if job['move']:
            step('moving', 'Moving the files to %s and matching them…' % job['new'])
            moved = move_to(comicid, newid)
            if not moved['ok']:
                return step('failed', moved['note'])
            note = moved['note']
            if job['remove'] and moved['kept']:
                # removing it now would leave those files in a folder nothing watches
                return step('done', note + ' %s stays on your watchlist because of them.' % job['old'], kept=moved['kept'], have=moved['have'])
        if job['remove']:
            step('removing', 'Removing %s from your watchlist…' % job['old'])
            if helpers.remove_series(comicid):
                note += ' Removed %s from your watchlist. Its folder was left alone.' % job['old']
                job['removed'] = True
            else:
                note += ' Couldn\'t remove %s. Check the log.' % job['old']
        step('done', note)
    except Exception as e:
        logger.error('[MATCHING] Wrong series %s -> %s failed: %s' % (comicid, newid, e))
        step('failed', 'That didn\'t work: %s' % e)
