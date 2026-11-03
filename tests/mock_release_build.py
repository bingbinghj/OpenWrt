"""Offline upstream fixtures for the customization integration test only."""
import importlib.util
import io
import os
from pathlib import Path
import sys
from unittest.mock import patch

from test_easytier_release import archive_payload, feed_makefile, release as easytier_release
from test_lucky_release import release as lucky_release


def main():
    action = sys.argv[1]
    if action == "prepare-packages":
        root = Path(sys.argv[2])
        for name in ("easytier", "easytier-noweb"):
            path = root / name / "Makefile"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(feed_makefile(name), encoding="utf-8", newline="\n")
        return
    if action == "prepare-lucky":
        path = Path(sys.argv[2]) / "Makefile"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PKG_NAME:=lucky\nPKG_VERSION:=2.17.8\nPKG_RELEASE:=1\n"
                        "PKG_BUILD_DIR:=$(BUILD_DIR)/$(PKG_NAME)-$(PKG_VERSION)\n"
                        "PKG_SOURCE_VERSION:=old\n\tDEPENDS:=@(x86_64||arm)\n"
                        "define Build/Prepare\n\twget old-version\nendef\n"
                        "define Build/Compile\nendef\n"
                        "define Package/$(PKG_NAME)/install\n"
                        "\t$(INSTALL_BIN) $(PKG_BUILD_DIR)/lucky $(1)/usr/bin/lucky\nendef\n",
                        encoding="utf-8", newline="\n")
        return
    script = Path(sys.argv[2])
    spec = importlib.util.spec_from_file_location("fixture_build_helper", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.argv = [str(script), *sys.argv[3:]]
    payload = archive_payload()
    if os.environ.get("FAIL_RELEASE") == "1":
        def fail():
            raise OSError("simulated latest stable API failure")
        module.fetch_release = fail
    elif script.name == "update_lucky.py":
        module.fetch_release = lucky_release
    else:
        module.fetch_release = lambda: easytier_release(payload)
    if script.name == "update_easytier.py":
        with patch.object(module.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
            module.main()
    else:
        module.main()


if __name__ == "__main__":
    main()
