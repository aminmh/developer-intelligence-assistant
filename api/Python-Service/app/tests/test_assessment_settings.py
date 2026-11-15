import pytest
from fastapi import HTTPException


def test_assessment_limit_persists_and_updates_runtime(monkeypatch, tmp_path):
    import infra.settings as settings_module
    import main
    from infra.settings_store import SettingsStore, build_config

    store = SettingsStore(str(tmp_path / 'settings.db'))
    monkeypatch.setattr(settings_module, 'settings_store', store)
    monkeypatch.setattr(main, 'settings_store', store)
    monkeypatch.setattr(settings_module.settings._env, 'assessment_request_token_limit', 8000)
    assert settings_module.settings.assessment_request_token_limit == 8000
    result = main.update_settings({'assessmentRequestTokenLimit': 24000})
    assert result['config']['assessmentRequestTokenLimit'] == 24000
    assert settings_module.settings.assessment_request_token_limit == 24000
    assert build_config(SettingsStore(str(tmp_path / 'settings.db')))['assessmentRequestTokenLimit'] == 24000
    main.update_settings({'assessmentRequestTokenLimit': 0})
    assert settings_module.settings.assessment_request_token_limit == 0
    for invalid in (-1, 1.5, True, '24000', 10_000_001):
        with pytest.raises(HTTPException):
            main.update_settings({'assessmentRequestTokenLimit': invalid})
