#!/bin/bash
# ─────────────────────────────────────────────────────────────────
# pmctl installatiescript
# Maakt een venv, installeert dependencies, en zet pmctl in ~/bin
# ─────────────────────────────────────────────────────────────────

set -e

PMCTL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/bin"
VENV_DIR="$PMCTL_DIR/venv"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  pmctl — Project Manager installatie"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Controleer Python
if ! command -v python3 &>/dev/null; then
    echo "✗  Python 3 niet gevonden. Installeer het eerst."
    exit 1
fi
PY_VERSION=$(python3 --version 2>&1)
echo "✓  $PY_VERSION gevonden"

# Venv aanmaken
if [ ! -d "$VENV_DIR" ]; then
    echo "▶  Virtuele omgeving aanmaken in $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
    echo "✓  Venv aangemaakt"
else
    echo "✓  Venv bestaat al"
fi

# Dependencies installeren
echo "▶  Dependencies installeren..."
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q -r "$PMCTL_DIR/requirements.txt"
echo "✓  Dependencies geïnstalleerd"

# ~/bin aanmaken indien nodig
mkdir -p "$BIN_DIR"

# Wrapper-script aanmaken
cat > "$BIN_DIR/pmctl" << WRAPPER
#!/bin/bash
# pmctl wrapper — gegenereerd door install.sh
source "$VENV_DIR/bin/activate"
exec python "$PMCTL_DIR/pmctl.py" "\$@"
WRAPPER
chmod +x "$BIN_DIR/pmctl"
echo "✓  pmctl geïnstalleerd in $BIN_DIR/pmctl"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Klaar!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Controleer of ~/bin in PATH zit
if [[ ":$PATH:" != *":$HOME/bin:"* ]]; then
    echo "  ⚠  ~/bin staat NIET in je PATH."
    echo "     Voeg dit toe aan je ~/.bashrc of ~/.zshrc:"
    echo ""
    echo '     export PATH="$HOME/bin:$PATH"'
    echo ""
    echo "  Of gebruik het directe pad:"
    echo "     $BIN_DIR/pmctl list"
else
    echo "  ~/bin staat al in je PATH — je kunt meteen beginnen:"
fi

echo ""
echo "  Snelstart:"
echo "    pmctl list              # alle projecten zien"
echo "    pmctl status            # gedetailleerde status"
echo "    pmctl start karelsassistant"
echo "    pmctl web               # dashboard op http://localhost:7777"
echo ""
echo "  Config: $PMCTL_DIR/projects.json"
echo ""
