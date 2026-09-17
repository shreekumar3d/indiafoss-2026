Updating the schedule:

create cfp-info.csv by exporting data from FOSS United. In this case, we select proposals
that are either in Screening or Approved state. We need ID, Title and Session Type fields.
Data is exported using dashboard for convenience, and care must be taken to ensure only
public fields are listed. Note that the exact same public data is avaiable on the "All
Proposals" page.

if2026-schedule.ods is the schedule, manually setup. Has scheduled talks, plus breaks.

python generate-schedule.py --schedule-script data.py

Open system console on FOSS United: https://fossunited.org/app/system-console/System%20Console

Copy-Paste the script and run. It clears all schedule entries, and populates the
current ones generated in the previous step. Also commits everything to the DB...

New schedule reflects in:

https://fossunited.org/dashboard/schedule/indiafoss/2026
