from v10_9_link_mode.connection_patch import install_connection_patch
from v10_9_link_mode.migration_patch import install_patch

install_connection_patch()
install_patch()

from v10_9_link_mode.main import main


if __name__ == "__main__":
    raise SystemExit(main())
