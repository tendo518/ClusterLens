"""Explicit NVIDIA-SMI index/UUID joins; no real commands or CUDA dependencies."""

import importlib.util
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

REPORTER = Path(__file__).resolve().parents[1] / "python/reporter.py"
spec = importlib.util.spec_from_file_location("nvidia_index_reporter_test", REPORTER)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

INDEX_COMMAND = [
    "nvidia-smi",
    "--query-gpu=index,uuid",
    "--format=csv,noheader,nounits",
]
XML_COMMAND = ["nvidia-smi", "-q", "-x"]
XML = """<nvidia_smi_log><driver_version>555.1</driver_version>
<gpu><uuid>GPU-two</uuid><minor_number>0</minor_number><product_name>Second</product_name>
<utilization><gpu_util>70 %</gpu_util></utilization></gpu>
<gpu><uuid>GPU-one</uuid><minor_number>1</minor_number><product_name>First</product_name>
<utilization><gpu_util>0 %</gpu_util></utilization></gpu></nvidia_smi_log>"""


class NvidiaIndexTests(unittest.TestCase):
    def collect(self, output="2, GPU-one\n7, GPU-two\n", xml=XML, **patches):
        def command(arguments, **kwargs):
            if arguments == INDEX_COMMAND:
                return subprocess.CompletedProcess(arguments, 0, output, "")
            self.assertEqual(arguments, XML_COMMAND)
            return subprocess.CompletedProcess(arguments, 0, xml, "")

        with (
            patch.object(agent.shutil, "which", return_value="nvidia-smi"),
            patch.object(agent.subprocess, "run", side_effect=command) as run,
            patch.dict(os.environ, patches),
        ):
            result = agent.gpu_data()
        return result, run

    def test_index_uuid_join_ignores_minor_numbers_and_xml_order(self):
        (gpus, warnings), run = self.collect()
        self.assertFalse(warnings)
        self.assertEqual(
            [(g["index"], g["uuid"], g["name"], g["utilization"]) for g in gpus],
            [(2, "GPU-one", "First", 0), (7, "GPU-two", "Second", 70)],
        )
        self.assertEqual(
            [call.args[0] for call in run.call_args_list], [INDEX_COMMAND, XML_COMMAND]
        )

    def test_missing_or_invalid_minor_numbers_do_not_change_explicit_index(self):
        for minor in ("", "<minor_number>N/A</minor_number>"):
            with self.subTest(minor=minor):
                xml = XML.replace("<minor_number>0</minor_number>", minor)
                (gpus, warnings), _ = self.collect(xml=xml)
                self.assertFalse(warnings)
                self.assertEqual([g["index"] for g in gpus], [2, 7])

    def test_cuda_visibility_does_not_filter_or_renumber_the_host_query(self):
        for visible in ("7,2", "GPU-two", "", "-1"):
            with self.subTest(visible=visible):
                (gpus, warnings), run = self.collect(CUDA_VISIBLE_DEVICES=visible)
                self.assertFalse(warnings)
                self.assertEqual([g["index"] for g in gpus], [2, 7])
                self.assertEqual(run.call_args_list[0].args[0], INDEX_COMMAND)

    def test_invalid_or_ambiguous_index_rows_never_fall_back(self):
        for output in (
            "0\n",
            "0, GPU-one, extra\n",
            "index, uuid\n",
            "-1, GPU-one\n",
            "1.5, GPU-one\n",
            "True, GPU-one\n",
            "1024, GPU-one\n",
            "0, N/A\n",
            "0, \n",
            "0, GPU-" + "a" * 125 + "\n",
            "0, GPU-one\n0, GPU-two\n",
            "0, GPU-one\n1, GPU-one\n",
            "0, GPU-one\n0, GPU-one\n",
            '"0, GPU-one\n',
            "\n",
            "\n".join(f"{i}, GPU-{i}" for i in range(129)),
        ):
            with self.subTest(output=output):
                (gpus, warnings), run = self.collect(output=output)
                self.assertEqual(gpus, [])
                self.assertTrue(warnings)
                self.assertEqual(run.call_count, 1)

    def test_uuid_set_changes_or_missing_identity_never_use_xml_order(self):
        for output, xml in (
            ("", XML),
            ("2, GPU-one\n", XML),
            ("2, GPU-one\n7, GPU-two\n9, GPU-three\n", XML),
            ("2, GPU-one\n7, GPU-two\n", XML.replace("<uuid>GPU-two</uuid>", "")),
            ("2, GPU-one\n7, GPU-two\n", XML.replace("GPU-two", "GPU-one")),
        ):
            with self.subTest(output=output, xml=xml):
                (gpus, warnings), _ = self.collect(output=output, xml=xml)
                self.assertEqual(gpus, [])
                self.assertTrue(warnings)

    def test_index_zero_and_upper_bound_are_preserved(self):
        (gpus, warnings), _ = self.collect(output="1023, GPU-two\n0, GPU-one\n")
        self.assertFalse(warnings)
        self.assertEqual([g["index"] for g in gpus], [0, 1023])

    def test_both_successful_queries_without_devices_are_cpu_only(self):
        (gpus, warnings), _ = self.collect(output="", xml="<nvidia_smi_log/>")
        self.assertEqual((gpus, warnings), ([], []))

    def test_queries_share_the_original_eight_second_budget(self):
        with patch.object(agent.time, "monotonic", side_effect=[100, 100, 102]):
            (gpus, warnings), run = self.collect()
        self.assertTrue(gpus)
        self.assertFalse(warnings)
        self.assertEqual(
            [call.kwargs["timeout"] for call in run.call_args_list], [8, 6]
        )
        self.assertTrue(
            all(call.kwargs["universal_newlines"] for call in run.call_args_list)
        )
        self.assertTrue(all(not call.kwargs["check"] for call in run.call_args_list))

    def test_exhausted_budget_does_not_start_the_xml_command(self):
        with patch.object(agent.time, "monotonic", side_effect=[100, 100, 109]):
            (gpus, warnings), run = self.collect()
        self.assertEqual(gpus, [])
        self.assertTrue(warnings)
        self.assertEqual(run.call_count, 1)

    def test_command_failures_are_warnings_without_a_numbering_fallback(self):
        for failure in (
            OSError("fixture"),
            subprocess.TimeoutExpired(INDEX_COMMAND, 8),
        ):
            with (
                self.subTest(failure=failure),
                patch.object(agent.shutil, "which", return_value="nvidia-smi"),
                patch.object(agent.subprocess, "run", side_effect=failure) as run,
            ):
                gpus, warnings = agent.gpu_data()
                self.assertEqual(gpus, [])
                self.assertTrue(warnings)
                self.assertEqual(run.call_count, 1)
        with (
            patch.object(agent.shutil, "which", return_value="nvidia-smi"),
            patch.object(
                agent.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    INDEX_COMMAND, 1, "", "fixture error"
                ),
            ) as run,
        ):
            gpus, warnings = agent.gpu_data()
        self.assertEqual(gpus, [])
        self.assertIn("fixture error", warnings[0])
        self.assertEqual(run.call_count, 1)

    def test_xml_command_failure_after_a_valid_mapping_is_a_warning(self):
        for failure in (
            subprocess.CompletedProcess(XML_COMMAND, 1, "", "fixture XML error"),
            subprocess.TimeoutExpired(XML_COMMAND, 6),
        ):
            with (
                self.subTest(failure=failure),
                patch.object(agent.shutil, "which", return_value="nvidia-smi"),
                patch.object(
                    agent.subprocess,
                    "run",
                    side_effect=[
                        subprocess.CompletedProcess(
                            INDEX_COMMAND, 0, "0, GPU-one\n", ""
                        ),
                        failure,
                    ],
                ) as run,
            ):
                gpus, warnings = agent.gpu_data()
                self.assertEqual(gpus, [])
                self.assertTrue(warnings)
                self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
