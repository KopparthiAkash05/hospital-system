// Shared helper: refresh a booking/reschedule time select from live availability.
function refreshTimeSlots(selectId, dateInputId, doctorId, excludeId, options) {
    var dateInput = document.getElementById(dateInputId);
    var select = document.getElementById(selectId);
    var hint = document.getElementById('slotHint');

    if (!dateInput || !select) return;

    if (!dateInput.value) return;

    var selected = new Date(dateInput.value + 'T00:00:00');
    if (selected.getDay() === 0) {
        select.innerHTML = '<option value="">-- Doctors are closed on Sundays --</option>';
        if (hint) hint.textContent = '* Please choose Monday to Saturday.';
        return;
    }

    var url = '/api/available_slots/' + doctorId + '?date=' + encodeURIComponent(dateInput.value);
    if (excludeId) url += '&exclude=' + excludeId;

    select.innerHTML = '<option value="">Loading availability...</option>';
    select.disabled = true;

    fetch(url, { headers: { 'Accept': 'application/json' } })
        .then(function (response) { return response.json(); })
        .then(function (data) {
            select.disabled = false;

            if (!data.success) {
                select.innerHTML = '<option value="">-- No availability --</option>';
                if (hint) hint.textContent = '* ' + (data.message || 'This date is not available.');
                return;
            }

            if (!data.slots || data.slots.length === 0) {
                select.innerHTML = '<option value="">-- No free slots on this date --</option>';
                if (hint) hint.textContent = '* ' + (data.message || 'Please choose another date.');
                return;
            }

            var options_html = '<option value="">-- Select Available Time --</option>';
            data.slots.forEach(function (slot) {
                options_html += '<option value="' + slot['24h'] + '">' + slot['12h'] + '</option>';
            });
            select.innerHTML = options_html;
            if (hint) hint.textContent = '* Only slots the doctor is available for are listed.';
        })
        .catch(function () {
            select.disabled = false;
            select.innerHTML = '<option value="">-- Could not load slots --</option>';
            if (hint) hint.textContent = '* Please reload the page and try again.';
        });
}