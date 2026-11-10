open_HOWI for Linux (x86-64)

  ./install.sh               install it for you: the app appears in your applications menu
  ./install.sh --uninstall   remove it again (your presets stay in ~/.config/openhowi)

Or run it from this folder: open_HOWI/open_HOWI (the app) or open_HOWI/openhowi (the command line).

To use the USB port, your user needs to be in the dialout group:
  sudo usermod -aG dialout $USER     (then log out and in again)

The app opens in its own window. If your system lacks the libraries for it, it opens in your web
browser instead. More: https://github.com/ccarl0/open_HOWI
