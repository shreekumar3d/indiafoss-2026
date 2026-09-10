EVENT = "FOSS Chapter Event"
event_name = "ek0supi1tu"  # constant

event = frappe.get_doc(EVENT, event_name)
event.set("event_schedule", [])
for track_name in tracks:
    for slot in tracks[track_name]:
        event.append('event_schedule',slot)
event.save()
frappe.db.commit()
