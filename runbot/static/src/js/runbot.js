(function($) {
    "use strict";

    $(function() {
        $('a.runbot-rebuild').click(function() {
            var $f = $('<form method="POST">'),
                url = _.str.sprintf('/runbot/build/%s/force', $(this).data('runbot-build')) + window.location.search;
            $f.attr('action', url);
            $f.appendTo($('body'));
            $f.submit();
            return false;
       });
    });
    $(function() {
        $('a.runbot-rebuild-exact').click(function() {
            var $f = $('<form method="POST">'),
                url = _.str.sprintf('/runbot/build/%s/force/1', $(this).data('runbot-build')) + window.location.search;
            $f.attr('action', url);
            $f.appendTo($('body'));
            $f.submit();
            return false;
       });
    });
    $(function() {
        $('a.runbot-kill').click(function() {
            var $f = $('<form method="POST">'),
                url = _.str.sprintf('/runbot/build/%s/kill', $(this).data('runbot-build')) + window.location.search;
            $f.attr('action', url);
            $f.appendTo($('body'));
            $f.submit();
            return false;
       });
    });
    $(function() {
        $('a.runbot-wakeup').click(function() {
            var $f = $('<form method="POST">'),
                url = _.str.sprintf('/runbot/build/%s/wakeup', $(this).data('runbot-build')) + window.location.search;
            $f.attr('action', url);
            $f.appendTo($('body'));
            $f.submit();
            return false;
       });
    });
    $(function() {
      new Clipboard('.clipbtn');
    });
})(jQuery);
