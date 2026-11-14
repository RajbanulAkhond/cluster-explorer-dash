/* Render shared Python-generated notation tokens as escaped React text.
 * Do not interpret HTML/Markdown or mutate the grid's raw values.
 */
(function () {
    const renderers = window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {};
    renderers.CEScientificText = function (props) {
        const tokens = props.data?.scientificParts?.[props.colDef.field];
        if (!tokens) return props.valueFormatted ?? String(props.value ?? "Unknown");
        const raw = Array.isArray(props.value) ? props.value.join(", ") : String(props.value ?? "Unknown");
        return React.createElement("span", {className: "ce-scientific", title: raw},
            ...tokens.map((token, index) => {
                const tag = token.script === "sub" || token.script === "sup" ? token.script : "span";
                return React.createElement(tag, {
                    key: index, className: token.script === "overline" ? "ce-overline" : undefined
                }, token.text);
            }));
    };
}());
