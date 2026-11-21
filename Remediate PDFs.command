#!/bin/zsh
# Double-click in Finder to make a folder of PDFs and Word (.docx) files accessible.
#
#   Double-click            → pick a folder in a dialog. A new folder: paste where the files are
#                             posted (optional), then it runs. A folder done before: a menu to
#                             process it again, attach files to a PDF, see where every document
#                             stands, rebuild the reports, or redo specific files.
#   Drag a folder onto it   → (in Terminal) "Remediate PDFs.command" ~/Downloads/<folder>
#   Extra options pass through to a11y_pipeline.py (and skip the menu), e.g.
#     "Remediate PDFs.command" ~/Downloads/<folder> --only "Addendum 3" --force
#
# After a run it offers to attach files (companion documents: a sheet index, a spreadsheet
# version of a table …) to any of the PDFs; they are embedded in the PDF and the documents are
# updated (attachments.py).
#
# Deliverables land in output/<folder>/ (opened when the run ends); the full log in logs/.

setopt extended_glob
cd "${0:A:h}" || exit 1
ROOT=$PWD
if [[ -x ".venv/bin/python" ]]; then
  PY=.venv/bin/python
else
  PY=$(command -v python3 || command -v python)
fi
[[ -n "$PY" ]] || { echo "Python 3 not found on PATH."; exit 1; }
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

finish() {
  [[ -t 0 ]] && { echo; read -rs "?Press Return to close this window."; echo; }
  exit ${1:-0}
}

# ---- the folder ----
if [[ -n "$1" && "$1" != --* ]]; then
  SOURCE=${1:A}
  shift
else
  SOURCE=$(osascript -e 'POSIX path of (choose folder with prompt "Choose the folder of PDFs to make accessible:")' 2>/dev/null)
  [[ -z "$SOURCE" ]] && { echo "No folder chosen."; finish 1; }
  SOURCE=${SOURCE%/}
fi
[[ -d "$SOURCE" ]] || { echo "Not a folder: $SOURCE"; finish 1; }

pdfs=("$SOURCE"/(^\~\$)*.(#i)(pdf|docx)(N))
others=("$SOURCE"/(^\~\$)*~*.(#i)(pdf|docx)(N.))
echo "Folder: ${SOURCE/#$HOME/~} · ${#pdfs} PDF / Word file(s)"
if (( ${#others} )); then
  echo "  not handled (post as-is, or save as .docx or PDF first):"
  for f in $others; do echo "    - ${f:t}"; done
fi
(( ${#pdfs} )) || { echo "No PDF or Word files in that folder."; finish 1; }

SLUG=$($PY -c "import re,sys; print(re.sub(r'[^a-z0-9]+','-',sys.argv[1].lower()).strip('-')[:60])" "${SOURCE:t}")
MANIFEST="manifests/$SLUG.yaml"

# ---- running the pipeline ----
# The window shows the pipeline's own progress; the Java/Tesseract chatter and pdf-a11y's step
# lines (thousands of lines on a big set) only go to the log. The pipeline prints the summary,
# sends the notification (A11Y_NOTIFY) and asks before a very large Claude run (A11Y_CONFIRM);
# colour is stripped from the log afterwards.
NOISE='^[A-Z][a-z]{2} [0-9]+, 20[0-9]{2} .*[AP]M org\.|^(WARNING|INFO|SEVERE): |\[tesseract\]|stringData|fitz. API|^\[[0-9]+[a-z]?/9\] |^Saving remediated PDF|^Done: /|^Consider using the pymupdf_layout'
run_pipeline() {
  mkdir -p logs
  local log="logs/$SLUG-$(date +%Y%m%d-%H%M%S).log"
  echo "This can take from minutes to hours; the Mac stays awake and the window title shows progress."
  A11Y_COLOR=1 A11Y_NOTIFY=1 A11Y_CONFIRM=1 A11Y_LOG=$log caffeinate -i $PY -u a11y_pipeline.py --source "$SOURCE" "$@" 2>&1 \
    | tee "$log" | grep --line-buffered -v -E "$NOISE"
  local rc=${pipestatus[1]}
  perl -pi -e 's/\e\[[0-9;]*m//g' "$log"
  if (( rc != 0 )); then
    osascript -e "display notification \"Stopped with an error: ${SOURCE:t}\" with title \"PDF accessibility\" sound name \"Basso\"" 2>/dev/null
    echo; echo "The run stopped with an error (exit $rc). See $log."
  fi
  RAN=1
  return $rc
}

# Attach files to PDFs (interactive, attachments.py), then update the documents that changed.
attach_files() {
  local changed=$(mktemp -t a11y-attach)
  $PY attachments.py "$MANIFEST" --changed-out "$changed"
  local rc=$?
  if (( rc == 0 )) && [[ -s "$changed" ]]; then
    echo
    echo "Embedding the attachments in $(wc -l < "$changed" | tr -d ' ') document(s) and checking them again…"
    run_pipeline --names-from "$changed"
    rc=$?
  fi
  rm -f "$changed"
  return $rc
}

RAN=0
rc=0
OFFER=0  # offer to attach files after a processing run
if [[ -f "$MANIFEST" && -t 0 && $# -eq 0 ]]; then
  echo "  using $MANIFEST (edit it to change titles, kinds or links)"
  while true; do
    echo
    echo "What would you like to do?"
    echo "  1  Process the folder: new and changed files (finished work is reused)"
    echo "  2  Attach files (companion documents) to a PDF"
    echo "  3  Show where every document stands"
    echo "  4  Rebuild the reports only"
    echo "  5  Redo specific files from scratch"
    echo "  q  Quit"
    read -r "?Choice [1]: " choice
    case ${choice:-1} in
      1) run_pipeline; rc=$?; OFFER=1; break ;;
      2) attach_files; rc=$? ;;
      3) A11Y_COLOR=1 $PY a11y_pipeline.py --source "$SOURCE" --status 2>&1 | grep -v -E "$NOISE" ;;
      4) run_pipeline --stages report; rc=$?; OFFER=1; break ;;
      5) read -r "?Part of the file name(s), separated by commas: " names
         [[ -n "$names" ]] && { run_pipeline --only "$names" --force; rc=$?; OFFER=1; break } ;;
      q|Q) break ;;
      *) echo "  Type 1–5, or q." ;;
    esac
  done
else
  # ---- where the files are posted (only asked when the folder is new) ----
  site_args=()
  if [[ ! -f "$MANIFEST" && " $* " != *" --site-url "* && " $* " != *" --crawl "* && -t 0 ]]; then
    echo
    echo "Where are these files posted? Paste the calendar event and/or Box folder link(s),"
    echo "separated by spaces, or a site section to search (ending in /, e.g. https://example.edu/documents/)."
    read -r "?Links (Return to skip): " links
    for url in ${=links}; do
      if [[ $url == */ && $url != */event/* && $url != *box.com* ]]; then
        site_args+=(--crawl "$url")
      else
        site_args+=(--site-url "$url")
      fi
    done
  elif [[ -f "$MANIFEST" ]]; then
    echo "  using $MANIFEST (edit it to change titles, kinds or links)"
  fi
  run_pipeline $site_args "$@"
  rc=$?
  OFFER=1
fi

if (( RAN && rc == 0 && OFFER )) && [[ -t 0 && -f "$MANIFEST" ]]; then
  echo
  read -r "?Attach files (companion documents) to any of these PDFs? [y/N] " yn
  [[ $yn == [yY]* ]] && { attach_files; rc=$? }
fi

OUT=$($PY -c "
import re, sys, yaml
name = yaml.safe_load(open(sys.argv[1]))['project']['name']
print('output/' + re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:60])" "$MANIFEST" 2>/dev/null)
(( RAN && rc == 0 )) && [[ -d "$OUT" && -t 0 ]] && open "$OUT"
finish $rc
