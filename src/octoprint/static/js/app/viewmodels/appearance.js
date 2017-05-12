$(function() {
    function AppearanceViewModel(parameters) {
        var self = this;

        self.name = parameters[0].appearance_name;
        self.color = parameters[0].appearance_color;
        self.colorTransparent = parameters[0].appearance_colorTransparent;

        self.brand = ko.pureComputed(function() {
            if (self.name())
                return self.name();
            else
                return gettext("ArtıBoyut");
        });

        self.fullbrand = ko.pureComputed(function() {
            if (self.name())
                return gettext("ArtıBoyut") + ": " + self.name();
            else
                return gettext("ArtıBoyut");
        });

        self.title = ko.pureComputed(function() {
            if (self.name())
                return self.name() + " [" + gettext("ArtıBoyut") + "]";
            else
                return gettext("ArtıBoyut");
        });
    }

    OCTOPRINT_VIEWMODELS.push([
        AppearanceViewModel,
        ["settingsViewModel"],
        "head"
    ]);
});
