import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import dalicontrol.profiles as profiles
from dalicontrol.adaptive_engine import AdaptiveEngine


def participant_info(score=3):
    return {
        "age_group": "30-39",
        "glasses_or_contacts": "yes",
        "brighter_lighting_preference": score,
    }


def final_payload(score=4):
    payload = {f"q{i}": score for i in range(1, 11)}
    payload["comments"] = "Lighting felt consistent."
    return payload


class ProfileStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_profiles_dir = profiles.PROFILES_DIR
        self.old_profiles_path = profiles.PROFILES_PATH
        self.old_lock = profiles._lock
        root = Path(self.tmp.name)
        profiles.PROFILES_DIR = root / "profiles"
        profiles.PROFILES_PATH = root / "profiles.json"
        profiles._lock = threading.Lock()

    def tearDown(self):
        profiles.PROFILES_DIR = self.old_profiles_dir
        profiles.PROFILES_PATH = self.old_profiles_path
        profiles._lock = self.old_lock
        self.tmp.cleanup()

    def test_create_list_select_and_active_profile_persistence(self):
        first = profiles.create_profile("Participant 01", participant_info(2))
        second = profiles.create_profile("Participant 02", participant_info(4))

        registry = profiles.list_profiles()
        self.assertEqual([p["profile_id"] for p in registry["profiles"]], [
            first["profile_id"],
            second["profile_id"],
        ])
        self.assertEqual(registry["active_profile_id"], second["profile_id"])

        selected = profiles.select_profile(first["profile_id"])
        self.assertEqual(selected["profile_id"], first["profile_id"])
        self.assertEqual(profiles.get_active_profile()["profile_id"], first["profile_id"])

        info_path = profiles.participant_info_path(first["profile_id"])
        self.assertTrue(info_path.exists())
        with info_path.open("r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["profile_name"], "Participant 01")
        self.assertEqual(saved["brighter_lighting_preference"], 2)

    def test_participant_info_validation_and_brightness_mapping(self):
        self.assertEqual(profiles.brightness_preference_from_score(1), 30.0)
        self.assertEqual(profiles.brightness_preference_from_score(3), 60.0)
        self.assertEqual(profiles.brightness_preference_from_score(5), 90.0)

        with self.assertRaises(ValueError):
            profiles.validate_participant_info({
                "age_group": "70-79",
                "glasses_or_contacts": "yes",
                "brighter_lighting_preference": 3,
            })

        with self.assertRaises(ValueError):
            profiles.validate_participant_info({
                "age_group": "18-29",
                "glasses_or_contacts": "maybe",
                "brighter_lighting_preference": 3,
            })

        with self.assertRaises(ValueError):
            profiles.validate_participant_info({
                "age_group": "18-29",
                "glasses_or_contacts": "no",
                "brighter_lighting_preference": 6,
            })

    def test_final_evaluation_validation_and_append_only_storage(self):
        profile = profiles.create_profile("Participant 03", participant_info(5))

        first = profiles.save_final_evaluation(
            profile["profile_id"],
            final_payload(4),
            mode="ai",
            telemetry_run="run_20260411_120000_ai.csv",
        )
        time.sleep(0.001)
        second = profiles.save_final_evaluation(
            profile["profile_id"],
            final_payload(5),
            mode="manual",
            telemetry_run="run_20260411_121000_manual.csv",
        )

        self.assertNotEqual(first["filename"], second["filename"])
        directory = profiles.final_evaluations_dir(profile["profile_id"])
        files = sorted(directory.glob("final_*.json"))
        self.assertEqual(len(files), 2)

        with (directory / first["filename"]).open("r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["profile_id"], profile["profile_id"])
        self.assertEqual(saved["mode"], "ai")
        self.assertEqual(saved["telemetry_run"], "run_20260411_120000_ai.csv")
        self.assertEqual(saved["answers"]["q10"], 4)
        self.assertEqual(saved["answers"]["comments"], "Lighting felt consistent.")

    def test_final_evaluation_rejects_missing_required_answers(self):
        profile = profiles.create_profile("Participant 04", participant_info(3))
        payload = final_payload()
        del payload["q10"]

        with self.assertRaises(ValueError):
            profiles.save_final_evaluation(profile["profile_id"], payload)

        self.assertEqual(profiles.list_final_evaluations(profile["profile_id"]), [])

    def test_profile_specific_model_paths_and_engine_switch(self):
        first = profiles.create_profile("Participant 05", participant_info(1))
        first_model_dir = profiles.profile_model_dir(first["profile_id"])
        engine = AdaptiveEngine(
            lamp=object(),
            lamp_lock=threading.Lock(),
            model_dir=first_model_dir,
            profile_id=first["profile_id"],
            profile_name=first["display_name"],
        )

        engine._models_loaded = True
        engine._brightness_model = object()
        engine._cct_model = object()

        second = profiles.create_profile("Participant 06", participant_info(5))
        second_model_dir = profiles.profile_model_dir(second["profile_id"])
        engine.set_profile_context(
            preferences=profiles.preference_adapter_for(second["profile_id"]),
            model_dir=second_model_dir,
            profile_id=second["profile_id"],
            profile_name=second["display_name"],
        )

        self.assertEqual(engine.profile_id, second["profile_id"])
        self.assertEqual(engine.profile_name, "Participant 06")
        self.assertEqual(engine._model_dir, second_model_dir)
        self.assertFalse(engine._models_loaded)
        self.assertIsNone(engine._brightness_model)
        self.assertIsNone(engine._cct_model)
        self.assertEqual(engine.preferences.get_preferred_brightness(12), 90.0)


if __name__ == "__main__":
    unittest.main()
