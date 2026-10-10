$(document).ready(function() {
    const menu = $('#menu-accordion');
    const storageKey = 'openedMenus';
    const openedClassName = 'show';

    // Remembers for every section (by its data-menu-key) whether it was open.
    // Sections without a remembered state keep their default from the template.
    let saved = {};
    try {
        saved = JSON.parse(localStorage.getItem(storageKey)) || {};
    } catch {
        saved = {};
    }

    function setToggleState(section, opened) {
        const toggle = menu.find('[data-bs-target="#' + section.attr('id') + '"]');
        toggle.toggleClass('collapsed', !opened);
        toggle.attr('aria-expanded', opened ? 'true' : 'false');
    }

    menu.find('.card-collapse').each(function() {
        const section = $(this);
        const key = section.data('menu-key');
        if (key in saved) {
            section.toggleClass(openedClassName, saved[key]);
        }
        setToggleState(section, section.hasClass(openedClassName));
    });

    function remember(event, opened) {
        const section = $(event.target);
        if (!section.hasClass('card-collapse')) {
            return;
        }
        saved[section.data('menu-key')] = opened;
        try {
            localStorage.setItem(storageKey, JSON.stringify(saved));
        } catch {
            // Storage may be unavailable (e.g. private mode); just don't remember.
        }
    }

    menu.on('shown.bs.collapse', function(event) { remember(event, true); });
    menu.on('hidden.bs.collapse', function(event) { remember(event, false); });
});
