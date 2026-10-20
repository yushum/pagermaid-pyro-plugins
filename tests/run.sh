set -eu

test "${PGM_PLUGIN_TESTING:-}" = "1"
mkdir -p data
cp config.gen.yml data/config.yml
python -m unittest discover -s /review/tests -v
