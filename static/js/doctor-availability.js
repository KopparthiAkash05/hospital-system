// Live availability for the doctor listings.
//
// The status rendered by the server is already correct for the moment the page
// was built, but a shift can start or end while the page stays open. This
// script polls the availability endpoint and updates the badges in place so a
// stale "Available" never lingers.
(function () {
    'use strict';

    var REFRESH_INTERVAL_MS = 60000;

    function badges() {
        return document.querySelectorAll('[data-availability-badge][data-doctor-id]');
    }

    function statusNote(info) {
        var note = info.reason || '';
        if (info.status === 'Available') {
            if (info.until) note += ' until ' + info.until;
        } else if (info.next) {
            note += ' \u00b7 Next: ' + info.next;
        }
        return note;
    }

    function applyBadge(badge, info) {
        var isAvailable = info.status === 'Available';
        var text = info.label || (isAvailable ? 'Available' : 'Not Available');

        if (badge.textContent.trim() !== text) {
            badge.textContent = text;
        }

        if (badge.classList.contains('badge')) {
            badge.classList.toggle('badge-approved', isAvailable);
            badge.classList.toggle('badge-rejected', !isAvailable);
        } else {
            var wrapper = badge.closest('.availability');
            if (wrapper) {
                wrapper.classList.toggle('availability--available', isAvailable);
                wrapper.classList.toggle('availability--unavailable', !isAvailable);
            }
        }

        var holder = badge.parentElement;
        var note = holder ? holder.querySelector('[data-availability-note]') : null;
        if (note) {
            var text2 = statusNote(info);
            if (note.textContent.trim() !== text2) note.textContent = text2;
        }
    }

    function refresh() {
        if (!badges().length) return;

        fetch('/api/doctor_availability', {
            headers: { 'Accept': 'application/json' },
            cache: 'no-store'
        })
            .then(function (response) {
                if (response.status === 401 || response.status === 403) return null;
                return response.json();
            })
            .then(function (data) {
                if (!data || !data.success || !data.doctors) return;

                var byId = {};
                data.doctors.forEach(function (entry) {
                    byId[entry.doctor_id] = entry;
                });

                badges().forEach(function (badge) {
                    var doctorId = badge.getAttribute('data-doctor-id');
                    var info = byId[doctorId];
                    if (info) applyBadge(badge, info);
                });
            })
            .catch(function () {
                // Keep the server rendered status when the refresh fails.
            });
    }

    function start() {
        refresh();
        window.setInterval(refresh, REFRESH_INTERVAL_MS);

        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) refresh();
        });
        window.addEventListener('focus', refresh);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
