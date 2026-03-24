# Anthem RS232 Plugin for Indigo

Control Anthem processors (Statement D1, D2, AVM50) via RS232 using Indigo and a Global Caché IP2SL or direct serial connection.

---

## ✨ Features

- ✅ Zone 1 / Zone 2 / Zone 3 control
- ✅ Power, Source, Volume, Mute control
- ✅ Proper per-zone command handling
- ✅ Channel trim control (Front, Center, Surround, Sub, LFE, etc.)
- ✅ Trim step up/down controls
- ✅ Processing mode selection (Dolby, DTS, THX, AnthemLogic, etc.)
- ✅ Live processing mode feedback
- ✅ Record output (Zone 4) control
- ✅ Configurable debug logging (RS232 TX/RX visibility)
- ✅ Clean, organized Action Groups (Z1 / Z2 / Z3)

---

## 🔌 Requirements

- Indigo 2025.1 or later
- Anthem processor with RS232 control:
  - Statement D1
  - Statement D2
  - AVM50
- Serial connection:
  - Global Caché IP2SL (recommended)
  - or direct serial interface

---

## ⚙️ Installation

1. Download the latest release `.indigoPlugin.zip`
2. Double-click the file to install into Indigo
3. Restart Indigo (if prompted)

---

## 🛠 Configuration

1. Create a new device:
   - **Plugin** → Anthem RS232
2. Enter:
   - IP address of your Global Caché IP2SL
   - Port (typically `4999`)
3. Select processor model:
   - D1
   - D2 / AVM50

The plugin will automatically create Zone 2 and Zone 3 devices.

---

## 🎛 Usage

### Control via Action Groups
- Z1 / Z2 / Z3 Basic controls (Power, Volume, Source)
- Z1 Trim controls (Set + Step)
- Z1 Processing modes
- Record output routing

### Control Pages
You can build control pages using:
- Volume
- Source
- Processing Mode
- Trim adjustments

---

## 🔍 Debug Logging

The plugin includes selectable logging levels:

- **Normal**
  - Clean logs (no RS232 traffic)
- **Debug**
  - Shows full TX/RX serial communication

Useful for troubleshooting or development.

---

## 🔄 Status Refresh

- Device “Send Status Request” button triggers:
  - Full refresh on main processor
  - Zone-specific refresh on Zone devices

---

## 🧠 Notes

- Zone 1 uses `P1VM` volume commands  
- Zone 2 uses `P2V` command family  
- Zone 3 uses `P3V` command family  
- Command handling varies slightly per zone — this plugin accounts for those differences

---

## 📦 Release

See [Releases](../../releases) for downloadable plugin versions.

---

## 📜 License

This project is licensed under the MIT License - see the LICENSE file for details.

---

## 🙌 Credits

Developed by Korey Sherwin  
Built with help from real-world testing and Anthem RS232 protocol documentation
