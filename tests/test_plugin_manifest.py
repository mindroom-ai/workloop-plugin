from __future__ import annotations

import json
from pathlib import Path

import yaml
from mindroom.tool_system.skills import list_skill_listings


def test_plugin_declares_workloop_template_skill() -> None:
    plugin_root = Path(__file__).resolve().parents[1]
    manifest = json.loads((plugin_root / "mindroom.plugin.json").read_text())

    assert "skills" in manifest
    assert "skills" in manifest["skills"]

    skill_path = plugin_root / "skills" / "workloop-templates" / "SKILL.md"
    assert skill_path.exists()

    content = skill_path.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(content.split("---", 2)[1])

    assert frontmatter["name"] == "workloop-templates"
    assert "<agent-workspace>/workloop/templates/" in content
    assert "Extra fields are rejected" in content
    assert "Normal todos must not include `sub_template` or `params`" in content
    assert "Sub-template todos must not include `title`, `priority`, or `assigned_agent`" in content
    assert "1-based indexes into the current rendered template's own `todos` list" in content
    assert "`mindroom-dev`: requires `ISSUE_REF: str`" in content
    assert "include_builtin_templates: false" in content

    listings = list_skill_listings([plugin_root / "skills"])
    assert [(listing.name, listing.path) for listing in listings] == [
        ("workloop-templates", skill_path)
    ]
