from __future__ import annotations

import unittest

from cell_note_agent.step_api import executable_skills


class ExecutableSkillFilterTests(unittest.TestCase):
    def test_excludes_skills_marked_as_planned_or_unavailable(self) -> None:
        skills = [
            {"name": "ready", "description": "implemented"},
            {"name": "references", "description": "placeholder", "status": "planned"},
            {"name": "disabled", "description": "not usable", "status": "unavailable"},
        ]

        self.assertEqual(
            [item["name"] for item in executable_skills(skills)],
            ["ready"],
        )

    def test_skills_without_status_remain_executable_for_compatibility(self) -> None:
        skills = [{"name": "legacy", "description": "existing skill"}]

        self.assertEqual(executable_skills(skills), skills)


if __name__ == "__main__":
    unittest.main()
