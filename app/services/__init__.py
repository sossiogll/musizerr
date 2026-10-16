"""
Layer dell'host: casi d'uso condivisi da routes/ e workers/, cosi' la
logica non e' duplicata tra i lati sincrono e asincrono, e i componenti
dell'host stesso (es. ``provider_registry.py``, che scopre e istanzia i
provider).

Puo' importare core/, db/, i package sotto providers/ (per la discovery) e
workers.celery_app; mai routes/ ne' i moduli dei task (workers.tasks,
workers.maintenance, ...).
"""
