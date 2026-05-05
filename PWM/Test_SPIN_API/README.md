# Test SPIN API PWM

Ce dossier contient les scripts Python de verification PWM pour le SPIN.

Contenu :
- `scripts/` : scripts de test PWM
- `lib/` : classes partagees pour piloter le CUT, le MUX et l'oscilloscope
- `PWM_Architecture.md` : vue d'ensemble de l'architecture des tests

Suites disponibles :
- `test_pwm_burst_mode.py`
- `test_pwm_dead_time.py`
- `test_pwm_duty_cycle.py`
- `test_pwm_frequency.py`
- `test_pwm_phase_shift.py`
- `test_pwm_rise_fall.py`

Chaque script utilise les classes de `lib/` :
- `cut_pwm.py`
- `mux_controller.py`
- `oscilloscope.py`
- `thingset.py`

Pre-requis materiels :
- une carte CUT accessible sur un port serie
- une carte MUX accessible sur un port serie
- un oscilloscope Rigol accessible via VISA

Lancement direct d'un script :

```powershell
python .\scripts\test_pwm_duty_cycle.py --cut-port COM22 --mux-port COM20 --cut-pin PA8 --channel ch1 --freq 1000 --duty-values 0.20 0.70
```

Lancement automatise via bench PlatformIO :

```powershell
cd ..\bench
$env:BENCH_CUT_PORT = "COM22"
$env:BENCH_MUX_PORT = "COM20"
```

Commandes disponibles :

```powershell
pio test -e bench -f pwm/test_duty_cycle
pio test -e bench -f pwm/test_frequency
pio test -e bench -f pwm/test_rise_fall
pio test -e bench -f pwm/test_dead_time
pio test -e bench -f pwm/test_phase_shift
pio test -e bench -f pwm/test_burst_mode
```

Si `pio` n'est pas disponible dans le `PATH`, utilise :

```powershell
py -m platformio test -e bench -f pwm/test_frequency
```

Le detail des variables d'environnement bench est documente dans le dossier voisin `../bench`.
