(function($) {
    "use strict";

    // maps classes to URL segments
    var CLASS_MAP = {
        'runbot-rebuild': 'force',
        'runbot-rebuild-exact': 'force/1',
        'runbot-kill': 'kill',
        'runbot-wakeup': 'wakeup'
    };
    $(function() {
        var $f = $('<form method="POST">').appendTo($('body'));
        // might be better to use a common selection class w/ a data attribute for the operation type
        $(document).on('click', '.runbot-rebuild, .runbot-rebuild-exact, .runbot-kill, .runbot-wakeup', function (e) {
            e.preventDefault();

            var segment = _(this.getAttribute('class').split('/\s+/')).chain()
                .map(function (c) { return CLASS_MAP[c]; })
                .find(_.identity);
            if (!segment) { return; }

            $f.attr('action', _.str.sprintf(
                '/runbot/build/%s/%s',
                $(this).data('runbot-build'),
                segment
            ) + window.location.search);
            $f.submit();
        });
    });
    $(function() {
      new Clipboard('.clipbtn');
    });
})(jQuery);
