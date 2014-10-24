(function($) {
    "use strict";

    $(function() {
        $('a.runbot-rebuild').click(function() {
            var $f = $('<form method="POST">'),
                url = _.str.sprintf('/runbot/build/%s/force', $(this).data('runbot-build'));
            $f.attr('action', url);
            $f.appendTo($('body'));
            $f.submit();
            return false;
       });
    });

})(jQuery);
