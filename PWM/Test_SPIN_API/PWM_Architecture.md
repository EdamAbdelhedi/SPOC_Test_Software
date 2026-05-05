# Architecture des tests PWM

Les tests PWM sont organises en deux couches :

- `lib/` : couche de pilotage materiel
- `scripts/` : couche de verification metier

## Roles

`cut_pwm.py`
- configure le PWM du CUT via ThingSet

`mux_controller.py`
- route le signal du CUT vers le bon canal du shield

`oscilloscope.py`
- configure le Rigol et lit les mesures

`thingset.py`
- fournit les primitives de communication ThingSet

## Sequence generale

1. le script se connecte au CUT, au MUX et a l'oscilloscope
2. le CUT genere le PWM demande
3. le MUX route le signal vers le canal choisi
4. le Rigol mesure la grandeur attendue
5. le script decide `PASS` ou `FAIL`

## Scripts inclus

- `scripts/test_pwm_burst_mode.py`
- `scripts/test_pwm_dead_time.py`
- `scripts/test_pwm_duty_cycle.py`
- `scripts/test_pwm_frequency.py`
- `scripts/test_pwm_phase_shift.py`
- `scripts/test_pwm_rise_fall.py`
