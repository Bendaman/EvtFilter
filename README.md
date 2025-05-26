# EvtFilter

**EvtFilter** is a high‑performance Incident Response utility that converts Windows `.evt` / `.evtx` event‑log files into a single, clean CSV for rapid triage.

* **Recursive** directory scan – point it at a case folder and go.
* **Time‑window** extraction – only the events between `--start-date` and `--end-date` are kept.
* **Event‑ID include / exclude** filters – whitelist or blacklist specific event numbers.
* **Delimiter‑proof** CSV – commas inside fields are replaced with a safe placeholder (default `§`).
* **Parallel** conversion – spawns one LogParser instance per CPU core (configurable).
* **Graceful error handling** – corrupt or empty logs are skipped and noted in a side log file.

The repo ships with **Microsoft Log Parser 2.2** in `./LogParser.exe`, so you don’t need to install anything system‑wide.

---

## Requirements

| Component | Version                   |
| --------- | ------------------------- |
| Python    | 3.8 – 3.12                |
| pandas    | ≥ 1.3  (2.2+ recommended) |
| lxml      | any                       |

Install the Python deps:

```powershell
pip install -r requirements.txt    # pandas, lxml
```

`requirements.txt`:

```text
pandas>=1.3
lxml
```

---

## Usage

```powershell
python evt_filter.py --dir <INPUT_DIR> `
    --start-date "YYYY-MM-DD HH:MM:SS" `
    --end-date   "YYYY-MM-DD HH:MM:SS" `
    --output     <OUTPUT_CSV> [options]
```

### Common options

| Switch                     | Purpose                                             |
| -------------------------- | --------------------------------------------------- |
| `--workers N`              | Parallel LogParser workers (default: CPU cores – 1) |
| `--event-ids 4624,4625`    | **Include** only these EventID values               |
| `--event-ids-file ids.txt` | Same as above, one ID per line                      |
| `--exclude-event-ids …`    | **Exclude** these EventIDs                          |
| `--placeholder-char §`     | Replacement char for in‑field commas                |
| `--log-file run.log`       | Write parsing errors here (default: `<output>.log`) |

### Example – grab RDP logons during an attack window

```powershell
python evt_filter.py --dir D:\IR\Case42\evtx `
    --start-date "2025-05-01 10:00:00" --end-date "2025-05-01 14:00:00" `
    --event-ids 4624,4625 --output rdp_window.csv
```

### Output files

* \`\` – merged, delimiter‑safe event log
* \`\` – INFO/ERROR lines for each input file (empty logs, corrupt files, etc.)

---

## Troubleshooting

| Symptom                           | Resolution                                                                                                     |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `LogParser.exe not found`         | Ensure the bundled binary is still in `./LogParser/`. Point to a custom path via `--logparser` if you move it. |
| `utf-8 codec can't decode byte`   | Old pandas – upgrade to ≥ 2.2 or keep the script’s shim.                                                       |
| `xpath does not return any nodes` | No events inside the selected time window – INFO line only, not an error.                                      |
| Script is slow                    | Increase `--workers`, run from SSD, or mount the case folder locally rather than over SMB.                     |

---

## License

MIT.

---

## Acknowledgements

* Microsoft Log Parser 2.2 – the unsung hero of DFIR.
* pandas & lxml – for turning scary XML into tidy tables.
