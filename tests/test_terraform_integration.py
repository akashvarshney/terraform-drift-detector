import json
from pathlib import Path
import shutil
import unittest

from drift_agent.main import scan


@unittest.skipUnless(shutil.which("terraform"), "Terraform is not installed")
class TerraformIntegrationTests(unittest.TestCase):
    def test_real_refresh_only_outcomes_without_cloud_access(self):
        fixtures = Path(__file__).parent / "fixtures"
        for fixture, status, exit_code in (
            ("clean", "clean", 0),
            ("output_change", "drift", 2),
            ("missing_variable", "error", 1),
        ):
            with self.subTest(fixture=fixture):
                repository = fixtures / fixture
                original_files = sorted(path.relative_to(repository) for path in repository.rglob("*"))
                report = scan(repository, timeout=30)
                self.assertEqual(report["status"], status, report)
                self.assertEqual(report["terraform_exit_code"], exit_code, report)
                self.assertNotIn("fixture-only-sensitive-value", json.dumps(report))
                self.assertEqual(original_files, sorted(path.relative_to(repository) for path in repository.rglob("*")))
                if status == "drift":
                    self.assertEqual(report["outputs"], [{"name": "sample", "actions": ["create"]}])


if __name__ == "__main__":
    unittest.main()