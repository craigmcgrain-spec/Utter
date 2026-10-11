from utter import paths


def test_config_path_honours_utter_override(tmp_xdg):
    assert paths.config_path() == tmp_xdg / "config" / "utter" / "config.toml"


def test_socket_path_is_under_runtime_dir(tmp_xdg):
    assert paths.socket_path() == tmp_xdg / "runtime" / "utter.sock"


def test_log_path_is_under_state_directory(tmp_xdg):
    assert paths.log_path() == tmp_xdg / "state" / "utter" / "utter.log"
