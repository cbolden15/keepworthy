from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import load_config
from src import poll, social


def settings(tmp_path, extra=''):
    path=tmp_path/'config.env'
    path.write_text('PLAYLIST_URL=https://www.youtube.com/playlist?list=example\nVAULT_DIR=/example/vault\n'+extra)
    return path


def test_plain_youtube_config_has_no_root_dependency(tmp_path, monkeypatch):
    monkeypatch.setattr('src.model_sandbox.available', lambda: False)
    cfg=load_config(settings(tmp_path))
    assert not cfg.model_sandbox and not cfg.free_media_enabled


@pytest.mark.parametrize('extra', ['SURE_CONTEXT_CONFIG=/private/sure.json\n', 'FREE_MEDIA_ENABLED=true\n'])
def test_private_integrations_require_explicit_sandbox(tmp_path, extra):
    with pytest.raises(ValueError,match='MODEL_SANDBOX'):
        load_config(settings(tmp_path,extra))


def test_enabled_sandbox_fails_at_startup_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr('src.model_sandbox.available', lambda: False)
    with pytest.raises(ValueError,match='root-owned model launcher'):
        load_config(settings(tmp_path,'MODEL_SANDBOX=true\n'))


def test_model_selection_reaches_both_live_entrypoints():
    cfg=SimpleNamespace(model_sandbox=True)
    assert poll._default_deps(cfg).run_claude.keywords['sandbox'] is True
    assert social._default_deps(cfg).run_claude_social.keywords['sandbox'] is True


def test_root_install_configuration_is_outside_the_request_protocol():
    import importlib.util
    path=Path(__file__).parents[1]/'bin/model_sandbox.py'
    spec=importlib.util.spec_from_file_location('public_helper',path)
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    request={'version':1,'operation':'summarize','model':'sonnet','prompt':'P','payload':'T','images':[],
             'operator_uid':1234,'auth_file':'/arbitrary'}
    with pytest.raises(ValueError,match='request-fields'):
        helper.validate(request)


def test_public_ci_is_valid_yaml_with_pinned_actions_and_no_private_runner_spend():
    import re
    import yaml
    workflow=yaml.load((Path(__file__).parents[1]/'.github/workflows/checks.yml').read_text(),Loader=yaml.BaseLoader)
    assert workflow['permissions']=={'contents':'read'}
    assert set(workflow['on'])=={'push','pull_request','workflow_dispatch'}
    for job in workflow['jobs'].values():
        assert job['if']=='${{ !github.event.repository.private }}'
        for step in job['steps']:
            if 'uses' in step:
                assert re.fullmatch(r'[^@]+@[a-f0-9]{40}',step['uses'])
            if 'with' in step and 'persist-credentials' in step['with']:
                assert step['with']['persist-credentials']=='false'

def test_ci_installs_are_hash_pinned_wheels_and_capture_job_adds_the_extra():
    import yaml
    workflow=yaml.load((Path(__file__).parents[1]/'.github/workflows/checks.yml').read_text(),Loader=yaml.BaseLoader)
    jobs=workflow['jobs']
    assert {'tests','tests-capture'}<=set(jobs)
    installs={}
    for name in ('tests','tests-capture'):
        runs=[step['run'] for step in jobs[name]['steps'] if 'pip install' in step.get('run','')]
        assert len(runs)==1, name
        assert '--require-hashes' in runs[0] and '--only-binary=:all:' in runs[0]
        assert any(step.get('run','').strip()=='python -m pytest -q' for step in jobs[name]['steps'])
        installs[name]=runs[0]
    assert '-r requirements-dev.lock' in installs['tests']
    assert 'requirements-capture.lock' not in installs['tests']
    assert '-r requirements-dev.lock -r requirements-capture.lock' in installs['tests-capture']
