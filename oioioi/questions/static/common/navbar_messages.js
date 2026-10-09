// Refreshes the "new messages" badge in the navbar with every status update,
// so new questions and answers show up without reloading the page.
$(function () {
    $(window).on('updateStatus', function (event, data) {
        if (!data || !('messages_badge' in data)) {
            return;
        }
        var current = $('#contest_new_messages');
        var badge = $.trim(data.messages_badge);
        if (current.length) {
            if (badge) {
                current.replaceWith(badge);
            } else {
                current.remove();
            }
        } else if (badge) {
            $('.oioioi-navbar__extra').prepend(badge);
        }
    });
});
