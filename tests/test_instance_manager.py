"""
tests/test_instance_manager.py

Settings → Save and the modem instances (vara/instance_manager.py): new,
re-enabled, turned-off, removed and changed instances are handled without
a restart of the BBS.  Starting and stopping a modem is recorded, not done.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import AppConfig, InstanceConfig
from core.events import EventBus
import vara.instance_manager as imod


def _cfg(*instances):
    cfg = AppConfig()
    cfg.instances = list(instances)
    return cfg


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(imod, "STARTUP_STAGGER_SECS", 0)
    calls = []
    im = imod.ModemInstanceManager(
        _cfg(InstanceConfig(name="FM", cmd_port=8300, data_port=8301),
             InstanceConfig(name="HF", transport="VARA_HF", cmd_port=8310,
                            data_port=8311, bandwidth=2300),
             InstanceConfig(name="OFF", cmd_port=8320, data_port=8321,
                            enabled=False)),
        EventBus())
    monkeypatch.setattr(im, "_start_instance", lambda s: calls.append(("start", s.cfg.name)))
    monkeypatch.setattr(im, "_stop_instance", lambda s: calls.append(("stop", s.cfg.name)))
    monkeypatch.setattr(im, "restart_instance", lambda n: calls.append(("restart", n)))
    return im, calls


def _apply(im, cfg, calls, expect):
    im.update_config(cfg)
    deadline = time.time() + 3
    while len(calls) < expect and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.1)
    return list(calls)


def test_new_and_reenabled_instances_start_without_a_restart(manager):
    """Instances not enabled at startup were skipped until a restart."""
    im, calls = manager
    cfg = _cfg(*[i.model_copy() for i in im._config.instances])
    cfg.instances[2].enabled = True                            # OFF on
    cfg.instances.append(InstanceConfig(name="PKT", transport="SOUNDMODEM",
                                        cmd_port=8330, data_port=8331))
    done = _apply(im, cfg, calls, 2)
    assert done == [("start", "OFF"), ("start", "PKT")]
    assert im.get_status("PKT") is not None


def test_turned_off_and_removed_instances_stop(manager):
    """A disabled or deleted instance kept running."""
    im, calls = manager
    cfg = _cfg(im._config.instances[0].model_copy(update={"enabled": False}))
    done = _apply(im, cfg, calls, 2)
    assert sorted(done) == [("stop", "FM"), ("stop", "HF")]
    assert im.get_status("FM") is None and im.get_status("HF") is None


def test_a_renamed_instance_frees_its_ports_before_the_new_one_starts(manager):
    im, calls = manager
    cfg = _cfg(im._config.instances[0].model_copy(update={"name": "FM2"}),
               im._config.instances[1])
    assert _apply(im, cfg, calls, 2) == [("stop", "FM"), ("start", "FM2")]


def test_a_new_instance_on_a_used_port_is_not_started(manager):
    im, calls = manager
    cfg = _cfg(*im._config.instances,
               InstanceConfig(name="DUP", cmd_port=8300, data_port=8301))
    assert _apply(im, cfg, calls, 1) == []
    assert im.get_status("DUP") is None


@pytest.mark.parametrize("change,restarts", [
    ({"ptt": {"method": "RTS"}}, ["HF"]),          # PTT is set up at start
    ({"bandwidth": 500}, ["HF"]),                  # BW is sent at init
    ({"frequency_label": "14.105"}, []),           # nothing to do
])
def test_hf_ptt_and_bandwidth_changes_restart_the_instance(manager, change, restarts):
    """PTT and VARA HF bandwidth were stored but used only after a restart."""
    im, calls = manager
    hf = im._config.instances[1]
    cfg = _cfg(im._config.instances[0],
               InstanceConfig.model_validate({**hf.model_dump(), **change}))
    done = _apply(im, cfg, calls, len(restarts))
    assert done == [("restart", n) for n in restarts]


def test_a_new_bbs_callsign_restarts_the_modems(manager):
    im, calls = manager
    cfg = im._config.model_copy(deep=True)
    cfg.station.bbs_callsign = "W9NEW-10"
    assert sorted(_apply(im, cfg, calls, 2)) == [("restart", "FM"), ("restart", "HF")]
