import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import voice_inject as V


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.pk = self.root / "DATA1.S13"
        self.offsets = [64, 16448, 8256, 24640]  # 故意乱序，最后一个物理槽用文件尾算容量。
        raw = bytearray(32832)
        for s, offset in enumerate(self.offsets):
            struct.pack_into("<II", raw, s * 8, 4 if s == 0 else 0, offset)
            payload = V.wrap_kovs(b"OggS" + bytes([s]) * 12)
            raw[offset:offset + len(payload)] = payload
        self.pk.write_bytes(raw)
        self.original = bytes(raw)
        self.export = self.root / "export.s13"
        export = bytearray(20)
        export[8:12] = bytes([1, 19, 2, 3])
        self.export.write_bytes(export)
        self.officer = {"name": "孙策", "id": 1, "voice": 19, "markers": ["孙策", "伯符"], "note": "fixture"}
        self.pack = self.root / "pack"
        self.pack.mkdir()
        for no in range(1, 10):
            (self.pack / V.SCENE_FILES[no]).write_bytes(f"wave-{no}".encode())
        self.out = self.root / "runs"
        self.out.mkdir()
        shutil.copyfile(V.PROJECT_ROOT / "data/buckets.json", self.data / "buckets.json")
        for key, value in dict(PK=self.pk, DATA=self.data, BASE=0, SLOT_COUNT=4, EXPORT=self.export,
                               OFFICER_BASE=16, OFFICER_STRIDE=1, OFFICER_COUNT=4, OUTROOT=self.out).items():
            self.enterContext(patch.object(V, key, value))
        self.enterContext(patch.object(V, "officers", return_value={"孙策": self.officer}))
        self.enterContext(patch.object(V, "_real_game_running", return_value=None))
        self.decoder = self.enterContext(patch.object(V, "ogg_seconds", return_value=1.0))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.bank = {"battle": [1, 1], "dialogue": [2, 2], "voice": 19, "verified": True}
        self.bank["source_sha256"] = V.bank_fingerprint(self.bank)
        self.save_bank()

    def save_bank(self):
        (self.data / "bank-map.json").write_text(json.dumps({"孙策": self.bank}), encoding="utf-8")

    def build(self):
        words = {n: {"words": [{"start": 0, "end": 1, "word": "测试。"}]} for n in range(1, 10)}
        with patch.object(V, "asr_pack_words", return_value=words), patch.object(V, "asr_slot_text", return_value="普通台词"):
            self.run_dir = V.cmd_plan(self.pack, "孙策")
        V.cmd_approve(self.run_dir, "孙策")
        with patch.object(V, "encode_clip", return_value=b"OggSnew-voice"):
            V.cmd_build(self.run_dir, "孙策")
        return self.run_dir

    def manifest(self):
        return json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))

    def test_unsorted_slot_uses_logical_address(self):
        self.assertEqual(V.slot_info(V.load_table(), 1), (16448, 16, 8192))
        self.assertEqual(V.extract_ogg(1), b"OggS" + b"\x01" * 12)

    def test_last_physical_slot_uses_file_end(self):
        self.assertEqual(V.slot_info(V.load_table(), 3)[2], 8192)

    def test_build_accepts_last_physical_slot(self):
        self.bank["dialogue"] = [3, 3]
        self.bank["source_sha256"] = V.bank_fingerprint(self.bank)
        self.save_bank()
        self.build()
        self.assertEqual(self.manifest()["slots"][-1]["gap"], 8192)

    def test_truncated_table_rejected(self):
        self.pk.write_bytes(self.original[:20])
        with self.assertRaises(SystemExit):
            V.load_table()

    def test_plan_and_build_keep_logical_addresses(self):
        self.build()
        self.assertEqual([m["abs_off"] for m in self.manifest()["slots"]], ["0x4040", "0x2040"])
        self.assertEqual((self.run_dir / "originals/slot1-orig.bin").read_bytes(), self.original[16448:24640])

    def test_missing_run_never_uses_latest_plan(self):
        self.build()
        with self.assertRaises(SystemExit):
            V.cmd_write(self.root / "typo", "孙策")
        self.assertEqual(self.pk.read_bytes(), self.original)

    def test_wrong_officer_rejected(self):
        self.build()
        with self.assertRaises(SystemExit):
            V.cmd_rollback("张角", self.run_dir)

    def test_unapproved_plan_rejected(self):
        self.build()
        plan_path = self.run_dir / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["approved"] = False
        plan_path.write_text(json.dumps(plan))
        with self.assertRaises(SystemExit):
            V.cmd_build(self.run_dir, "孙策")

    def test_edit_after_approval_rejected(self):
        self.build()
        path = self.run_dir / "plan.json"
        plan = json.loads(path.read_text())
        plan["slots"][0]["t0"] = 0.3
        path.write_text(json.dumps(plan))
        with self.assertRaises(SystemExit):
            V.cmd_build(self.run_dir, "孙策")

    def test_changed_wave_rejected_at_build(self):
        self.build()
        (self.pack / V.SCENE_FILES[1]).write_bytes(b"replacement-wave")
        with self.assertRaises(SystemExit):
            V.cmd_build(self.run_dir, "孙策")

    def test_write_rechecks_exclusivity(self):
        self.build()
        data = bytearray(self.export.read_bytes())
        data[8] = 19
        self.export.write_bytes(data)
        with self.assertRaises(SystemExit):
            V.cmd_write(self.run_dir, "孙策")
        self.assertEqual(self.pk.read_bytes(), self.original)

    def test_forged_manifest_offset_rejected(self):
        self.build()
        man = self.manifest()
        man["slots"][0]["abs_off"] = "0x2040"
        (self.run_dir / "manifest.json").write_text(json.dumps(man))
        with self.assertRaises(SystemExit):
            V.cmd_write(self.run_dir, "孙策")
        self.assertEqual(self.pk.read_bytes(), self.original)

    def test_later_slot_changed_leaves_all_slots_unwritten(self):
        self.build()
        altered = bytearray(self.original)
        altered[8256 + 40] ^= 1
        self.pk.write_bytes(altered)
        with self.assertRaises(SystemExit):
            V.cmd_write(self.run_dir, "孙策")
        self.assertEqual(self.pk.read_bytes(), altered)

    def test_later_payload_damaged_leaves_all_slots_unwritten(self):
        self.build()
        (self.run_dir / "kovs/slot2.kovs").write_bytes(b"damaged")
        with self.assertRaises(SystemExit):
            V.cmd_write(self.run_dir, "孙策")
        self.assertEqual(self.pk.read_bytes(), self.original)

    def test_write_and_rollback_roundtrip(self):
        self.build()
        V.cmd_write(self.run_dir, "孙策")
        self.assertNotEqual(self.pk.read_bytes(), self.original)
        self.assertEqual(self.pk.read_bytes()[:32], self.original[:32])
        V.cmd_rollback("孙策", self.run_dir)
        self.assertEqual(self.pk.read_bytes(), self.original)
        V.cmd_rollback("孙策", self.run_dir)
        self.assertEqual(self.pk.read_bytes(), self.original)

    def test_rollback_conflict_does_not_restore_earlier_slot(self):
        self.build()
        V.cmd_write(self.run_dir, "孙策")
        altered = bytearray(self.pk.read_bytes())
        altered[8256 + 40] ^= 1
        self.pk.write_bytes(altered)
        with self.assertRaises(SystemExit):
            V.cmd_rollback("孙策", self.run_dir)
        self.assertEqual(self.pk.read_bytes(), altered)

    def test_candidate_decode_failure_leaves_live_untouched(self):
        self.build()
        self.decoder.side_effect = [1.0, 1.0, SystemExit(1)]
        with self.assertRaises(SystemExit):
            V.cmd_write(self.run_dir, "孙策")
        self.assertEqual(self.pk.read_bytes(), self.original)

    def corrupt_candidate(self, mutate):
        verify = V.verify_candidate
        def corrupt(stage, rows, decode):
            data = bytearray(stage.read_bytes())
            mutate(data)
            stage.write_bytes(data)
            verify(stage, rows, decode)
        self.build()
        with patch.object(V, "verify_candidate", side_effect=corrupt), self.assertRaises(SystemExit):
            V.cmd_write(self.run_dir, "孙策")
        self.assertEqual(self.pk.read_bytes(), self.original)

    def test_non_target_change_in_same_hash_block_rejected(self):
        self.corrupt_candidate(lambda d: d.__setitem__(50, 1))

    def test_full_frame_table_change_rejected(self):
        self.corrupt_candidate(lambda d: d.__setitem__(8, 1))

    def test_one_byte_file_growth_rejected(self):
        self.corrupt_candidate(lambda d: d.extend(b"x"))

    def test_interrupt_before_replace_leaves_live_untouched(self):
        self.build()
        with patch.object(V.os, "replace", side_effect=OSError("simulated interruption")), self.assertRaises(OSError):
            V.cmd_write(self.run_dir, "孙策")
        self.assertEqual(self.pk.read_bytes(), self.original)
        self.assertEqual(json.loads((self.run_dir / "write-state.json").read_text())["state"], "ready")
        V.cmd_status(self.run_dir, "孙策")

    def test_interrupt_after_replace_is_detectable_and_rollback_works(self):
        self.build()
        write = Path.write_text
        def interrupted(path, text, *args, **kwargs):
            if path.name == "write-state.json" and '"committed"' in text:
                raise OSError("simulated interruption after replace")
            return write(path, text, *args, **kwargs)
        with patch.object(Path, "write_text", interrupted), self.assertRaises(OSError):
            V.cmd_write(self.run_dir, "孙策")
        V.cmd_status(self.run_dir, "孙策")
        V.cmd_rollback("孙策", self.run_dir)
        self.assertEqual(self.pk.read_bytes(), self.original)

    def test_game_running_blocks_write_and_rollback(self):
        self.build()
        with patch.object(V, "_real_game_running", return_value=42):
            for operation in (lambda: V.cmd_write(self.run_dir, "孙策"), lambda: V.cmd_rollback("孙策", self.run_dir)):
                with self.assertRaises(SystemExit):
                    operation()
        self.assertEqual(self.pk.read_bytes(), self.original)

    def test_partial_bank_rejected_without_scanning(self):
        self.bank["partial"] = True
        self.save_bank()
        with patch.object(V, "_scan") as scan, self.assertRaises(SystemExit):
            V.cmd_locate("孙策")
        scan.assert_not_called()

    def test_locate_never_marks_heuristic_candidate_verified(self):
        self.bank["verified"] = False
        self.save_bank()
        with patch.object(V, "_scan", return_value={1: "伯符", 2: "伯符"}), patch.object(V, "_refine", side_effect=[(1, 1), (2, 2)]):
            entry = V.cmd_locate("孙策")
        self.assertIs(entry["verified"], False)

    def test_locate_requires_two_anchors_in_each_region(self):
        self.bank["verified"] = False
        self.save_bank()
        with patch.object(V, "_scan", return_value={1: "伯符"}), self.assertRaises(SystemExit):
            V.cmd_locate("孙策")

    def test_confirm_bank_binds_current_source(self):
        V.cmd_confirm_bank("孙策", [1, 1], [2, 2])
        entry = V.bankmap()["孙策"]
        self.assertIs(entry["verified"], True)
        self.assertEqual(entry["source_sha256"], V.bank_fingerprint(entry))

    def test_reference_survives_repeated_locate_and_manual_confirmation(self):
        self.bank["verified"] = False
        self.save_bank()
        with patch.object(V, "_scan", return_value={1: "伯符", 2: "伯符"}), patch.object(V, "_refine", return_value=(1, 1)):
            first = V.cmd_locate("孙策")
            self.assertEqual(first["reference"]["previous"]["dialogue"], [2, 2])
            second = V.cmd_locate("孙策")
            self.assertEqual(second["reference"], first["reference"])
        V.cmd_confirm_bank("孙策", [1, 1], [2, 2])
        confirmed = V.bankmap()["孙策"]
        self.assertEqual(confirmed["reference"]["confirmed"]["dialogue"], [2, 2])
        confirmed["verified"] = False
        V.save_bankmap({"孙策": confirmed})
        with patch.object(V, "_scan", return_value={1: "伯符", 2: "伯符"}), patch.object(V, "_refine", return_value=(1, 1)):
            result = V.cmd_locate("孙策")
        self.assertEqual(result["reference"], confirmed["reference"])

    def test_shipped_reference_keeps_sunce_3510(self):
        bank = json.loads((V.PROJECT_ROOT / "data/bank-map.json").read_text(encoding="utf-8"))
        self.assertEqual(bank["孙策"]["reference"]["historical"]["dialogue"][0], 3510)
        self.assertEqual(bank["袁绍"]["reference"]["historical"]["battle"], [1733, 1762])

    def test_supported_traditional_officer_markers(self):
        for traditional, simplified in [("董仲穎", "董仲颖"), ("大賢良師", "大贤良师"),
                                        ("蒼天已死", "苍天已死"), ("貧道", "贫道"),
                                        ("古之惡來", "古之恶来"), ("鳳雛", "凤雏"),
                                        ("袁紹", "袁绍")]:
            with self.subTest(traditional=traditional):
                self.assertTrue(V._is_marker(traditional, [simplified]))

    def test_traditional_foreign_names_and_target_markers(self):
        self.assertTrue(V._is_foreign("趙雲在此", ["伯符"]))
        self.assertTrue(V._is_foreign("呂布在此", ["伯符"]))
        self.assertTrue(V._is_foreign("諸葛亮", ["伯符"]))
        self.assertFalse(V._is_foreign("孫伯符", ["伯符"]))

    def test_short_acknowledgement_no_longer_truncates_refine(self):
        text = {0: "趙雲", 1: "明白", 2: "伯符", 3: "呂布"}
        with patch.object(V, "asr_slot_text", side_effect=text.__getitem__):
            self.assertEqual(V._refine(2, 2, ["伯符"], 3), (1, 2))

    def test_asr_slot_cache_tracks_source_bytes(self):
        calls = []
        def transcribe(*args, **kwargs):
            calls.append(1)
            return iter([SimpleNamespace(text="result")]), None
        with patch.object(V, "_model", return_value=SimpleNamespace(transcribe=transcribe)), patch.object(V, "_decode_pcm_bytes", return_value=b"pcm"):
            V.asr_slot_text(1)
            V.asr_slot_text(1)
            data = bytearray(self.pk.read_bytes())
            data[16448 + 40] ^= 1
            self.pk.write_bytes(data)
            V.asr_slot_text(1)
        self.assertEqual(len(calls), 2)

    def test_same_pack_name_or_replaced_wave_does_not_reuse_stale_cache(self):
        calls = []
        def transcribe(*args, **kwargs):
            calls.append(1)
            return iter([SimpleNamespace(text="result", start=0, end=1, words=[])]), SimpleNamespace(duration=1)
        with patch.object(V, "_model", return_value=SimpleNamespace(transcribe=transcribe)), patch.object(V, "_decode_pcm", return_value=b"pcm"):
            V.asr_pack_words(self.pack)
            V.asr_pack_words(self.pack)
            other = self.root / "other/pack"
            shutil.copytree(self.pack, other)
            (other / V.SCENE_FILES[1]).write_bytes(b"changed")
            V.asr_pack_words(other)
        self.assertEqual(len(calls), 10)

    def test_character_markers_are_not_global_bucket_patterns(self):
        buckets = json.loads((self.data / "buckets.json").read_text(encoding="utf-8"))
        self.assertEqual(V._bucket_of("孙伯符在此", buckets, "battle", True, ["伯符"]), ("自报家门", 1))
        self.assertNotEqual(V._bucket_of("孙伯符在此", buckets, "battle", True, ["本初"])[1], 1)


class ProcessAndCodecTests(unittest.TestCase):
    def test_short_dialogue_uses_semantics_before_grunts(self):
        buckets = json.loads((V.DATA / 'buckets.json').read_text(encoding='utf-8'))
        for text, expected in [('遵命', ('日常领命', 11)), ('明白', ('日常领命', 11)),
                               ('多谢', ('日常致谢', 11)), ('我接受', ('兜底', 11))]:
            with self.subTest(text=text):
                self.assertEqual(V._bucket_of(text, buckets, 'dialogue', True), expected)
        self.assertEqual(V._bucket_of('我接受', buckets, 'dialogue'), ('接受', 3))
        self.assertEqual(V._bucket_of('多谢', buckets, 'dialogue'), ('接受', 3))

    def test_grunts_laughter_and_battle_commands_keep_their_buckets(self):
        buckets = json.loads((V.DATA / 'buckets.json').read_text(encoding='utf-8'))
        for ext in (False, True):
            for where in ('battle', 'dialogue'):
                with self.subTest(ext=ext, where=where):
                    self.assertEqual(V._bucket_of('哼！', buckets, where, ext), ('短喝', 10 if ext else 8))
                    self.assertEqual(V._bucket_of('哈哈', buckets, where, ext), ('笑', 10 if ext else 2))
        self.assertEqual(V._bucket_of('冲啊', buckets, 'battle', True), ('战场冲锋', 10))
        self.assertEqual(V._bucket_of('冲啊', buckets, 'battle'), ('出征', 7))

    def test_japanese_low_memory_process_is_detected(self):
        result = SimpleNamespace(returncode=0, stdout='"San13PK_jp.exe","42","Console","1","1 K"\n', stderr="")
        with patch.object(V.subprocess, "run", return_value=result):
            self.assertEqual(V._real_game_running(), 42)

    def test_tasklist_failure_and_malformed_output_block(self):
        for result in (SimpleNamespace(returncode=1, stdout="", stderr="denied"),
                       SimpleNamespace(returncode=0, stdout="unexpected", stderr="")):
            with self.subTest(result=result), patch.object(V.subprocess, "run", return_value=result), self.assertRaises(SystemExit):
                V._real_game_running()

    def test_cli_missing_arguments_has_usage_instead_of_indexerror(self):
        with patch.object(sys, "argv", ["voice_inject.py", "build"]), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as result:
                V.main()
        self.assertEqual(result.exception.code, 2)
        self.assertNotIn("IndexError", err.getvalue())

    def test_real_ffmpeg_kovs_roundtrip_and_corrupt_stream(self):
        encoded = subprocess.run([V.FF, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1",
                                  "-c:a", "libvorbis", "-f", "ogg", "-"], check=True, capture_output=True).stdout
        self.assertEqual(V.unwrap_kovs(V.wrap_kovs(encoded)), encoded)
        self.assertGreater(V.ogg_seconds(V.unwrap_kovs(V.wrap_kovs(encoded))), 0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            V.ogg_seconds(b"OggSbroken")


if __name__ == "__main__":
    unittest.main()

