function init_user_selection(id, num_hints) {
    $(function(){
        var input = $('#' + id);
        input.typeahead({
            source: function(query, process) {
                $.getJSON(input.data("hintsUrl"), {substr: query}, process);
            },
            items: num_hints,
            minLength: 2
        });
        // Picking a hint inside a dropdown (e.g. the su form) must not close it.
        input.data("typeahead").$menu.on("click", function(event) {
            event.stopPropagation();
        });
    });
}
