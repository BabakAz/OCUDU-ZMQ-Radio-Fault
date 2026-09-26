"""Deterministic whole-record logging regression, with the former print path."""
import builtins
import importlib
import io
import json
from pathlib import Path
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'RADIO_FIXED_PROFILE: '


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'scripts'))
    return importlib.import_module('ocudu_channel_broker'), importlib.import_module('radio_fault')


def profile(direction, kind):
    return PREFIX+json.dumps({'schema_version':'radio_fixed_profile_v1',
                             'direction':direction,'record_type':kind},separators=(',',':'))


class ControlledWrites:
    """Hold the first payload before print's separate newline write."""
    def __init__(self, first, second):
        self.first, self.second = first, second
        self.first_entered = threading.Event()
        self.second_entered = threading.Event()
        self.release_first = threading.Event()
        self.fragments = []
        self.lock = threading.Lock()

    def write(self, value):
        with self.lock:
            self.fragments.append(value)
        if value == self.first:
            self.first_entered.set()
            if not self.release_first.wait(2):
                raise TimeoutError('test did not release first print payload')
        if value == self.second:
            self.second_entered.set()
        return len(value)

    def flush(self):
        pass


def concurrent_output(printer, first, second, *, expect_serialized):
    stream = ControlledWrites(first,second)
    errors = []
    second_attempted = threading.Event()
    def write(value, second=False):
        try:
            if second:
                second_attempted.set()
            printer(value,file=stream,flush=True)
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=write,args=(first,)),
               threading.Thread(target=write,args=(second,True))]
    threads[0].start()
    try:
        assert stream.first_entered.wait(2)
        threads[1].start()
        assert second_attempted.wait(2)
        observed = stream.second_entered.wait(.1 if expect_serialized else 2)
        assert observed is (not expect_serialized)
    finally:
        stream.release_first.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(2)
    assert not errors and all(not thread.is_alive() for thread in threads)
    return ''.join(stream.fragments).encode()


def test_previous_print_reproduces_two_complete_json_records_on_one_line(modules):
    _,pilot = modules
    ul,dl = profile('UL','final'),profile('DL','final')
    raw = concurrent_output(builtins.print,ul,dl,expect_serialized=False)
    assert raw == (ul+dl+'\n\n').encode()
    with pytest.raises(pilot.FaultError,match='malformed GRC fixed profile JSON'):
        pilot.grc_fixed_profiles(raw)


def test_both_final_records_remain_separate_and_parser_stays_strict(modules):
    broker,pilot = modules
    raw = concurrent_output(broker.print,profile('UL','final'),profile('DL','final'),expect_serialized=True)
    raw = (profile('DL','started')+'\n'+profile('UL','started')+'\n').encode()+raw
    records = pilot.grc_fixed_profiles(raw)
    assert all(set(records[name]) == {'started','final'} for name in ('DL','UL'))
    assert raw.count(b'\n') == 4


@pytest.mark.parametrize('first_profile',[False,True])
def test_diagnostic_text_cannot_join_a_profile_payload(modules,first_profile):
    broker,_ = modules
    record,diagnostic = profile('UL','final'),'[GRC] Relay threads started'
    first,second = (record,diagnostic) if first_profile else (diagnostic,record)
    raw = concurrent_output(broker.print,first,second,expect_serialized=True)
    assert raw.splitlines() == [first.encode(),second.encode()]
    assert json.loads(next(x for x in raw.decode().splitlines() if x.startswith(PREFIX))[len(PREFIX):])['direction'] == 'UL'


def test_existing_print_format_and_file_arguments_are_unchanged(modules):
    broker,_ = modules
    actual,expected = io.StringIO(),io.StringIO()
    kwargs = dict(sep='|',end='\n\n',flush=True)
    broker.print('one',2,None,file=actual,**kwargs)
    builtins.print('one',2,None,file=expected,**kwargs)
    assert actual.getvalue() == expected.getvalue()
