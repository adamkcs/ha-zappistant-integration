# Zappistant

A Home Assistant integration that runs a weekly charging schedule for a myenergi **zappi**, on top of
the [myenergi integration](https://github.com/CJNE/ha-myenergi). It's the companion to
[Zappi Card](https://github.com/adamkcs/ha_zappisstant), which is where the schedule is edited.

- **Weekly schedule**: half-hour charging slots and a kWh amount per day, run even with no dashboard open
- **Overnight charges** across midnight as one charge
- **One-off adjustments** to the next charge (start/target %, or skip it) from the card
- **Predbat**: publishes the charging plan so Predbat can plan the house battery around it
- **Intelligent Octopus Go mode**: lets Octopus charge, and keeps its charge target and ready-by time set from your plan

## Requirements

- Home Assistant 2024.11 or later with the [myenergi integration](https://github.com/CJNE/ha-myenergi) set up
- [Zappi Card](https://github.com/adamkcs/ha_zappisstant) to edit the schedule
- For Octopus mode: the [Octopus Energy integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy)

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/adamkcs/ha-zappistant-integration` as type **Integration**
2. Install **Zappistant** and restart Home Assistant
3. Settings → Devices & services → **Add integration** → **Zappistant**

### Manual

Copy `custom_components/zappistant` to `<config>/custom_components/zappistant`, restart Home Assistant,
then add the integration as above.

### Setup

Pick the zappi's charge mode entity; its sensors are found automatically. Then choose **who controls
charging**:

- **Zappistant** (default): Zappistant boosts the zappi in your slots.
- **Intelligent Octopus Go**: Octopus charges; Zappistant sets its target (see below).

Also set the **charging power** (7 kW for a single-phase zappi, 22 kW for three-phase), used to
estimate charging slots for Predbat. Everything can be changed later under **Configure**.

## Weekly schedule

Edit the schedule in Zappi Card's editor (Edit card → **Weekly schedule**). It saves straight to
Zappistant with its own Save button. Each weekday has:

- **Charging slots**: tap or drag on a half-hour grid, up to 5 intervals per day (midnight to midnight)
- **Expected start %** and **target %**, which calculate the **kWh** from the card's battery
  calibration. You can also type the kWh in directly; it's the amount the schedule actually uses.
- **Keep charging after** (per interval): when on, charging continues past that interval's end until
  the day's kWh is delivered, even past midnight. When off, charging stops at the interval's end.

**Overnight charges**: when a day's last slot runs to 24:00 and the next day (also switched on)
starts with a 00:00 slot, the two are one charge, e.g. Tuesday 22:00–24:00 plus Wednesday
00:00–05:00 is a single 22:00–05:00 charge. It uses the first day's start %, target % and kWh, and
counts delivered energy across midnight. The next day's own charge is its remaining slots.

Zappistant runs the plan:

- At the start of the day's first slot it switches the zappi to Stopped (an option, on by default) so
  it doesn't charge outside the slots.
- In each slot it starts a boost for the kWh still needed, if the car is plugged in.
- At the end of a slot it stops the boost, unless that interval keeps charging after.
- It counts kWh delivered from the zappi's session energy sensor, so a day's later slots only add
  what's still missing. An overrun is dropped if it hasn't finished by the end of the next day.

The card's **Upcoming charge** line adjusts just the next charge: change its start or target % and
Apply, Reset to the weekly plan, or Skip it (with Undo until its slots have passed).

### Entities

- `sensor.zappi_schedule_status`: what the schedule is doing (idle, waiting, charging, done, …),
  with the run's progress and the upcoming charge as attributes
- `switch.zappi_schedule_enabled`: turns the whole schedule on or off
- `binary_sensor.zappi_schedule_charge_slot`: the charging plan for Predbat (below)

## Predbat

Zappistant publishes its charging plan in the format [Predbat](https://springfall2008.github.io/batpred/)
reads planned car charging from. `binary_sensor.zappi_schedule_charge_slot` is on during a planned
charge, and its `planned_dispatches` attribute lists the next 48 hours:

- **Schedule**: each upcoming charge split over its slots at the charging power, trimmed to the kWh
  still needed, and extended past a slot's end when "keep charging after" is on.
- **Boost now** (from the card): from now until the kWh is in.
- **Smart boost** (from the card): myenergi doesn't say when it will charge, so it's estimated as
  the latest block that finishes by the "Ready by" time.

Boosts are dropped from the plan once delivered, when the car is unplugged, when you press Stop,
or when the ready-by time passes. Boosts started outside the card (e.g. the myenergi app) aren't in
the plan; Predbat only sees them once the car is charging.

Each slot has the source `charger-schedule`, which Predbat treats as car load only, never as a
cheap tariff slot. In Predbat's `apps.yaml`:

```yaml
num_cars: 1
octopus_intelligent_slot: binary_sensor.zappi_schedule_charge_slot
```

Keep Predbat's own zappi control (`myenergi_zappi_control`) off, or it will switch the zappi
between Fast and Stopped against Zappistant. Predbat's myenergi support (or `car_charging_now` and
`car_charging_energy` pointed at the zappi's plug status and session energy) still covers a car that
is charging right now and keeps car energy out of the house load history.

In Octopus mode, point `octopus_intelligent_slot` at the Octopus Energy integration's own dispatch
sensor instead (Predbat's default). Zappistant's charge slot sensor then only lists boosts from the card.

## Intelligent Octopus Go (Octopus mode)

On Intelligent Octopus Go, Octopus can control the zappi itself and charge in its cheapest slots.
Choose **Intelligent Octopus Go** as "Who controls charging". A second screen asks for the Octopus
Energy integration's Intelligent **charge target** (`number.octopus_energy_…_intelligent_charge_target`)
and **target time** (`time.` or `select.octopus_energy_…_intelligent_target_time`), which are found
automatically, and the **battery size you entered in the Octopus app**.

With a zappi, Octopus can't see the car's battery %, so its charge target is the **% of the battery
to add**, not the level to stop at. Zappistant works that out from the charge's kWh (which includes
charging losses): kWh ÷ Octopus's battery size, rounded up and kept within Octopus's 5–100%. For
example, 45 → 80% at 0.581 kWh/% is 20.3 kWh, which on a 61 kWh battery is 34% to add.

In Octopus mode:

- Each weekday has a **ready-by time** (the next morning) instead of slots. Octopus picks the slots.
  Your slot plan is kept, so switching back restores it.
- Zappistant keeps Octopus's charge target and target time set for the upcoming charge: the
  evening's charge, until its ready-by time the next morning. If they're changed elsewhere (e.g. in
  the Octopus app), they're set back to the plan.
- Zappistant never boosts or switches the zappi to Stopped. Keep the zappi in **Eco+** for Octopus.
  Boost now from the card still works, at the normal rate.
- A skipped night sets Octopus's minimum of 5%. To skip charging completely, turn off smart charging
  in the Octopus app or don't plug in.

## Development

To try a change before releasing it, `scripts/deploy.sh` copies the integration over SSH into Home
Assistant and restarts it (after a configuration check). Copy `.deploy.env.example` to `.deploy.env`
and set your host first. `--no-restart` skips the restart and `--dry-run` shows what it would do.
The next HACS update restores the release.

Tests run the scheduling logic on its own and the integration inside Home Assistant (Python 3.13):

```sh
python3.13 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
```
