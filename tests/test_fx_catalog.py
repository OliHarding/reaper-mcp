"""Tests for offline FX & preset catalog parsing."""

from __future__ import annotations

from pathlib import Path

from reaper_interface.config import ReaperConfig
from reaper_interface.fx_catalog import list_cached_presets, list_installed_fx


def test_parse_mock_reaper_catalog(tmp_path: Path):
    (tmp_path / "reaper-vstplugins64.ini").write_text(
        "[vstcache]\n"
        "reasynth.dll=001,123,ReaSynth (Cockos)!!!VSTi\n"
        "reaeq.dll=002,124,ReaEQ (Cockos)\n"
        "Massive_X.vst3=003,125{ABC,Massive X (Native Instruments)!!!VSTi\n"
        "Raum.vst3=004,126{DEF,Raum (Native Instruments)\n"
        "Broken.vst3=005\n"
        "Shell.vst3=006,0,<SHELL>\n",
        encoding="utf-8",
    )
    (tmp_path / "reaper-clap-win64.ini").write_text(
        "[Surge XT.clap]\n"
        "_=HASH\n"
        "org.surge.xt=1|Surge XT (Surge Synth Team)\n"
        "[CHOWTapeModel.clap]\n"
        "_=HASH2\n"
        "org.chow.tape=0|CHOWTapeModel (chowdsp)\n",
        encoding="utf-8",
    )
    (tmp_path / "reaper-jsfx.ini").write_text(
        'VERSION 1\n'
        'NAME delay/delay "JS: Delay"\n'
        'NAME synth/sine "JS: Sine Synth"\n',
        encoding="utf-8",
    )
    chains = tmp_path / "FXChains"
    chains.mkdir()
    (chains / "VocalChain.RfxChain").write_text("<FXCHAIN>", encoding="utf-8")

    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "vst-ChowKick-builtin.ini").write_text(
        "[factory]\npreset_cnt=2\n00001=Default\n00002=Tight\n", encoding="utf-8"
    )
    (presets / "vst-ReaEQ.ini").write_text(
        "[General]\nNbPresets=1\n[Preset0]\nName=Warm Low Cut\n", encoding="utf-8"
    )

    cfg = ReaperConfig(
        reaper_resource_dir=tmp_path,
        ipc_root=tmp_path / "ipc",
        requests_dir=tmp_path / "ipc" / "requests",
        responses_dir=tmp_path / "ipc" / "responses",
        heartbeat_file=tmp_path / "ipc" / "heartbeat.json",
        scripts_install_dir=tmp_path / "Scripts",
    )

    all_fx = list_installed_fx(config=cfg)
    assert all_fx["total_catalog_count"] == 9  # 4 valid VST + 2 CLAP + 2 JSFX + 1 FXChain

    insts = list_installed_fx(kind="instrument", config=cfg)
    inst_names = {x["reaper_add_name"] for x in insts["items"]}
    assert "VST3i: Massive X (Native Instruments)" in inst_names
    assert "CLAPi: Surge XT (Surge Synth Team)" in inst_names
    assert "VSTi: ReaSynth (Cockos)" in inst_names
    assert "VST3: Raum (Native Instruments)" not in inst_names

    clap_fx = list_installed_fx(fmt="clap", kind="effect", config=cfg)
    assert len(clap_fx["items"]) == 1
    assert clap_fx["items"][0]["reaper_add_name"] == "CLAP: CHOWTapeModel (chowdsp)"

    preset_res = list_cached_presets(config=cfg)
    assert preset_res["matched_files"] == 2
    by_plugin = {p["plugin"]: p for p in preset_res["plugins"]}
    assert [x["name"] for x in by_plugin["vst-ChowKick"]["presets"]] == ["Default", "Tight"]
    assert [x["name"] for x in by_plugin["vst-ReaEQ"]["presets"]] == ["Warm Low Cut"]


def test_parse_macos_arm64_and_au_catalog(tmp_path: Path):
    (tmp_path / "reaper-vstplugins_arm64.ini").write_text(
        "[vstcache]\n"
        "Serum.vst3=001,123{ABC,Serum (Xfer Records)!!!VSTi\n"
        "ValhallaVintageVerb.vst3=002,124{DEF,ValhallaVintageVerb (Valhalla DSP)\n",
        encoding="utf-8",
    )
    (tmp_path / "reaper-clap-arm64.ini").write_text(
        "[Surge XT.clap]\n_=HASH\norg.surge.xt=1|Surge XT (Surge Synth Team)\n",
        encoding="utf-8",
    )
    (tmp_path / "reaper-auplugins_arm64.ini").write_text(
        "[aucache]\n"
        "Apple: AUSampler=001!aumu\n"
        "Apple: AUMatrixReverb=002!aufx\n",
        encoding="utf-8",
    )
    cfg = ReaperConfig(
        reaper_resource_dir=tmp_path,
        ipc_root=tmp_path / "ipc",
        requests_dir=tmp_path / "ipc" / "requests",
        responses_dir=tmp_path / "ipc" / "responses",
        heartbeat_file=tmp_path / "ipc" / "heartbeat.json",
        scripts_install_dir=tmp_path / "Scripts",
    )
    all_fx = list_installed_fx(config=cfg)
    assert all_fx["total_catalog_count"] == 5

    au_insts = list_installed_fx(fmt="au", kind="instrument", config=cfg)
    assert len(au_insts["items"]) == 1
    assert au_insts["items"][0]["reaper_add_name"] == "AUi: Apple: AUSampler"

    au_fx = list_installed_fx(fmt="au", kind="effect", config=cfg)
    assert len(au_fx["items"]) == 1
    assert au_fx["items"][0]["reaper_add_name"] == "AU: Apple: AUMatrixReverb"

