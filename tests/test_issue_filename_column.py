"""Issues tab: the file column reads Location, and the table header sits beside Status."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import mylar
from mylar import db, webserve


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def issues(monkeypatch, tmp_path):
    monkeypatch.setattr(mylar, 'DATA_DIR', str(tmp_path), raising=False)
    monkeypatch.setattr(mylar, 'CONFIG', SimpleNamespace(MULTIPLE_DEST_DIRS=None), raising=False)
    d = db.DBConnection()
    d.action('CREATE TABLE comics (ComicID TEXT, ComicLocation TEXT)')
    d.action('CREATE TABLE issues (IssueID TEXT, ComicName TEXT, IssueName TEXT, Issue_Number TEXT, Status TEXT, '
             'ComicID TEXT, ReleaseDate TEXT, Location TEXT, IssueDate TEXT, DigitalDate TEXT, Int_IssueNumber INT, '
             'ComicSize TEXT, AltIssueNumber TEXT)')
    d.action('INSERT INTO comics (ComicID, ComicLocation) VALUES (?, ?)', ['4050-1', str(tmp_path)])
    rows = [
        ('i1', '1', 1, 'Wanted', None),
        ('i2', '2', 2, 'Downloaded', 'annuals/Saga 002 (2012).cbz'),
        ('i3', '3', 3, 'Downloaded', 'Saga 003 (2012).cbz'),
    ]
    for issueid, number, n, status, location in rows:
        d.action('INSERT INTO issues (IssueID, ComicName, IssueName, Issue_Number, Status, ComicID, ReleaseDate, '
                 'Location, IssueDate, DigitalDate, Int_IssueNumber, ComicSize, AltIssueNumber) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 [issueid, 'Saga', 'Name ' + number, number, status, '4050-1', '2012-0%s-01' % number,
                  location, '2012-0%s-01' % number, '0000-00-00', n, '1200', None])
    return webserve.WebInterface()


def load(ui, **kwargs):
    params = dict(iDisplayStart=0, iDisplayLength=-1, iSortCol_0='1', sSortDir_0='desc', sSearch='')
    params.update(kwargs)
    return json.loads(ui.loadIssueDetails('4050-1', **params))['aaData']


@pytest.mark.unit
def test_location_stays_in_the_column_the_file_name_reads(issues):
    rows = {r[4]: r for r in load(issues)}
    assert rows['i1'][5] is None
    assert rows['i2'][5] == 'annuals/Saga 002 (2012).cbz'
    assert rows['i3'][5] == 'Saga 003 (2012).cbz'


@pytest.mark.unit
def test_filter_matches_the_file_name(issues):
    found = load(issues, sSearch='Saga 002')
    assert [r[4] for r in found] == ['i2']


@pytest.mark.unit
def test_file_column_sorts_by_location(issues):
    asc = [r[4] for r in load(issues, iSortCol_0='5', sSortDir_0='asc')]
    assert asc == ['i1', 'i2', 'i3']


@pytest.mark.unit
def test_issues_table_header_puts_file_beside_status():
    html = (ROOT / 'data/interfaces/default/comicdetails_update.html').read_text(encoding='utf-8')
    block = html[html.index('id="issue_table"'):html.index('id="annual_table"')]
    assert block.index('id="status"') < block.index('id="filename">File') < block.index('id="options"')
    assert 'function issueFileName(loc)' in html
    assert 'issueFileName(full[5])' in html
