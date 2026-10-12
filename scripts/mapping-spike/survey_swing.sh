#!/bin/bash
# Altitude swing inside each survey pass, from a wingman log (Design 017, phase 4b).
#
#   survey_swing.sh <wingman log>
#
# For every pass (from one "edge ahead" or mission start to the next) it takes
# the highest minus the lowest altitude on the per-tick line (alt=), in two
# windows: 12 to 35 s after the pass starts, which is the turn's aftermath, and
# from 35 s on, which is the settled part. One line for each window: the number
# of passes with at least six readings, and the quartiles.
#
# Figures to compare against (2026-10-04): 499 m median from 35 s on before the
# altitude hold was changed (99 passes), about 350 m after (14 and 15 passes).
log="$1"
tmp="$(mktemp)"
grep -E "BT\[active\].*mission=True|SURVEY: edge ahead|mission_survey - passes on" "$log" \
  | sed -E 's/\x1b\[[0-9;]*m//g' \
  | awk '{
      split($2, t, /[:,]/); sec = t[1] * 3600 + t[2] * 60 + t[3]
      if ($0 ~ /edge ahead|passes on/) {
        if (n2 >= 6) print hi2 - lo2, hi1 - lo1
        start = sec; n1 = 0; n2 = 0; next
      }
      alt = ""
      for (i = 1; i <= NF; i++) if ($i ~ /^alt=[0-9]/) { split($i, a, "="); alt = a[2] + 0 }
      if (alt == "" || start == "") next
      age = sec - start
      if (age >= 12 && age < 35) {
        if (n1 == 0) { hi1 = alt; lo1 = alt }
        if (alt > hi1) hi1 = alt; if (alt < lo1) lo1 = alt; n1++
      }
      if (age >= 35 && age < 75) {
        if (n2 == 0) { hi2 = alt; lo2 = alt }
        if (alt > hi2) hi2 = alt; if (alt < lo2) lo2 = alt; n2++
      }
    }
    END { if (n2 >= 6) print hi2 - lo2, hi1 - lo1 }' > "$tmp"
for col in 1 2; do
  sort -n -k"$col" "$tmp" | awk -v c="$col" '
    { a[NR] = $c }
    END {
      if (NR == 0) { print "no passes long enough"; exit }
      printf "%s: passes %d | swing 25th pct %.0f m, median %.0f m, 75th pct %.0f m\n",
        (c == 1) ? "from 35 s into the pass on" : "12 to 35 s into the pass   ",
        NR, a[int(NR / 4) + 1], a[int((NR + 1) / 2)], a[int(3 * NR / 4)]
    }'
done
rm -f "$tmp"
