# Anthem RS232 Plugin for Indigo

Control Anthem processors (Statement D1, D2, and AVM50) from Indigo over RS232 using a Global Caché IP2SL.

## Overview

This plugin provides Indigo control and status feedback for supported Anthem processors, including multi-zone operation, source selection, volume control, mute, processing mode selection, channel trims, and record output routing.

## Features

- Zone 1, Zone 2, and Zone 3 control
- Power, source, volume, and mute control
- Proper per-zone command handling
- Channel trim control for Front, Center, Surround, Sub, LFE, and more
- Trim step up and step down controls
- Processing mode selection including Dolby, DTS, THX, AnthemLogic, and others
- Live processing mode feedback
- Record output (Zone 4) control
- Configurable debug logging with RS232 TX/RX visibility
- Organized Indigo action groups for Z1, Z2, and Z3
- Status request button for processor and zone refresh

## Requirements

- Indigo 2025.1 or later
- Anthem processor with RS232 control:
  - Statement D1
  - Statement D2
  - AVM50
- Global Caché IP2SL serial adapter
- Network access to the IP2SL on TCP port 4999

## Installation

1. Download the latest plugin release zip.
2. Double-click the plugin zip or the included `.indigoPlugin` file to install it into Indigo.
3. Restart Indigo if prompted.

## Configuration

1. In Indigo, create a new device using the Anthem RS232 plugin.
2. Enter the IP address of your Global Caché IP2SL.
3. Set the port to `4999`.
4. Set the command terminator to `LF (\n)` if needed.
5. Select the processor model:
   - D1
   - D2 / AVM50

The plugin will automatically create the appropriate child zone devices.

## Usage

### Main Controls

The plugin supports the following control functions:

- Power on and off
- Source selection
- Volume up, down, and set
- Mute
- Processing mode selection
- Channel trim adjustment
- Record output routing

### Action Groups

You can use Indigo action groups for common tasks such as:

- Zone 1 basic controls
- Zone 2 basic controls
- Zone 3 basic controls
- Zone 1 trim controls
- Zone 1 processing mode changes
- Record output routing

### Control Pages

The plugin is suitable for Indigo control pages using states and actions such as:

- Volume
- Source
- Processing mode
- Trim adjustments
- Power status

## Status Refresh

The plugin includes a status request control for refreshing device state.

- On the main processor device, it triggers a full processor refresh.
- On zone devices, it triggers a zone-specific refresh.

## Debug Logging

The plugin supports selectable logging levels:

### Normal

Shows clean operational logs without raw RS232 traffic.

### Debug

Shows detailed RS232 transmit and receive logging for troubleshooting and development.

## Notes

- Zone 1 uses the `P1VM` volume command family.
- Zone 2 uses the `P2V` command family.
- Zone 3 uses the `P3V` command family.
- The plugin handles the command differences between zones automatically.

## Release Information

Current public repository release: `v0.3.32`. :contentReference[oaicite:1]{index=1}

For downloads and release notes, see the repository Releases section. :contentReference[oaicite:2]{index=2}

## License

This project is licensed under the MIT License. See the `LICENSE` file for details. :contentReference[oaicite:3]{index=3}

## Credits

Developed by Korey Sherwin.

Built through real-world testing with Anthem processors and Global Caché IP2SL hardware.
