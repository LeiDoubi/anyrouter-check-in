import subprocess
import tarfile
from io import BytesIO

from scripts.setup_cc_proxy import ArchiveSpec, build_archive_payload, build_setup_script


def test_generated_setup_script_installs_node_prereqs() -> None:
	script = build_setup_script([], 3000, 'https://proxy.example.test')

	assert 'install_system_node_prereqs()' in script
	assert 'ensure_node_install_prereqs()' in script
	assert 'apt-get install -y ca-certificates curl tar xz-utils' in script
	assert 'apk add --no-cache ca-certificates curl tar xz' in script
	assert 'if ! ensure_node_install_prereqs; then' in script


def test_generated_setup_script_has_valid_bash_syntax() -> None:
	script = build_setup_script([], 3000, 'https://proxy.example.test')

	result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True, check=False)

	assert result.returncode == 0, result.stderr


def test_generated_setup_script_extracts_codex_skills(tmp_path) -> None:
	source = tmp_path / 'skills'
	source.mkdir()
	(source / 'example').mkdir()
	(source / 'example' / 'SKILL.md').write_text('# Example\n', encoding='utf-8')
	payload = build_archive_payload(ArchiveSpec('Codex skills', source, '.codex/skills', required=False))

	script = build_setup_script([], 3000, 'https://proxy.example.test', [payload])

	assert 'write_archive_dir "${HOME}/.codex/skills" ' in script
	result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True, check=False)
	assert result.returncode == 0, result.stderr


def test_codex_skills_archive_dereferences_symlinked_skills(tmp_path) -> None:
	switch_skills = tmp_path / 'cc-switch-skills'
	switch_skills.mkdir()
	linked_skill = switch_skills / 'linked-skill'
	linked_skill.mkdir()
	(linked_skill / 'SKILL.md').write_text('# Linked Skill\n', encoding='utf-8')

	codex_skills = tmp_path / 'codex-skills'
	codex_skills.mkdir()
	(codex_skills / 'linked-skill').symlink_to(linked_skill, target_is_directory=True)

	payload = build_archive_payload(ArchiveSpec('Codex skills', codex_skills, '.codex/skills', required=False))

	with tarfile.open(fileobj=BytesIO(payload.content), mode='r:gz') as archive:
		member = archive.getmember('linked-skill/SKILL.md')
		content = archive.extractfile(member)
		assert content is not None
		assert content.read().decode('utf-8') == '# Linked Skill\n'
		assert archive.getmember('linked-skill').isdir()
