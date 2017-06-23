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
        $('a.runbot-kill').click(function() {
            var $f = $('<form method="POST">'),
                url = _.str.sprintf('/runbot/build/%s/kill', $(this).data('runbot-build')) + window.location.search;
            $f.attr('action', url);
            $f.appendTo($('body'));
            $f.submit();
            return false;
       });
    });

})(jQuery);
