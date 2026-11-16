# Epson ET-2810 waste ink pad counter reset

A single-file Python tool that reads and resets the waste ink pad counters on
an Epson EcoTank ET-2810 (also ET-2811 / 2812 / 2813 / 2814 / 2815 / 2816 /
2818). It talks to the printer over Wi-Fi; no drivers or extra packages needed.

## Requirements

- Python 3.8 or newer (Windows, macOS or Linux)
- The printer connected to the same network as your computer (USB-only is not
  supported)
- The printer's address: print a network status sheet from the printer, or
  look it up in your router's device list. The address Windows shows under
  the printer's properties also works; paste it in quotes, for example
  `"http://[fe80::a6d7:3cff:fe20:f2f%19]:80/WSD/DEVICE"`

## Usage

```
python3 epson_et2810_reset.py status 192.168.1.50      # look, change nothing
python3 epson_et2810_reset.py reset 192.168.1.50       # back up, then reset
```

Run `status` first. If it shows your model, serial number and counters, the
tool can talk to your printer and `reset` should work.

`reset` saves the cells it is about to change to `et2810-backup-<time>.json`,
asks you to type `RESET`, writes the new values, reads each one back to confirm
it, and then clears the latched "pad full" flag. Switch the printer off and on
afterwards.

Other commands:

```
python3 epson_et2810_reset.py restore 192.168.1.50 et2810-backup-....json
python3 epson_et2810_reset.py temp-reset 192.168.1.50
```

- `restore` writes a backup file back, undoing a reset.
- `temp-reset` asks the firmware to clear the "pad full" error without writing
  to the EEPROM. Use it when `status` or `reset` says the firmware blocks
  EEPROM access over the network (recent firmware does). It lasts only until
  the printer is switched off, so leave the printer on and run it again after
  each power-on. Add `--serial XXXXXXXXXX` with the serial from the printer's
  label if it cannot be read automatically.

## Before you reset

The counter exists because the pads really do fill with ink. Resetting it does
not empty them. Replace or wash and dry the pads, or fit an external waste ink
bottle, or ink will eventually leak out of the bottom of the printer.

The tool refuses to write to a printer that does not report an ET-281x model
name, because EEPROM addresses differ between models.

## Tests

```
python3 -m unittest -v
```

The tests run against a simulated printer included in the test file.

## Credits

EEPROM addresses and access keys come from the open-source
[epson_print_conf](https://github.com/Ircama/epson_print_conf) project.
