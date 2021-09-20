function sendStatusEvent(event, value) {

    sendEvent("status.set", [event, value])

}

function sendEvent(event, value = null) {
    if (value == null) {
        command = {
            "Event": event,
        }
    } else {
        command = {
            "Event": event,
            "Argument": value
        }
    }
    fetch('/command', {
        'body': JSON.stringify(command),
        'method': 'POST'
    })
}

self.sendEvent('routes.update', true)

$('button[data-type=showSubButton]').on('click', function(event) {


    target = $(event.target).data('target')
    expanded = $(event.target).data('expanded')

    if (expanded == false) {
        console.log(`showing ${target}`)
        $(target).show()
        $(event.target).data('expanded', true)

    } else {
        console.log(`hiding ${target}`)
        $(target).hide()
        $(event.target).data('expanded', false)
    }

})

routeList = document.createElement('ul')

fetch('/status/Functions/AutoPilot/Routes').then(response => {
    response.json().then(status => {
        status.forEach(function(val) {
            li = document.createElement('li')
            li.innerHTML = val
            routeList.append(li)
        })
    }).then(data => {
        $('#gpxRoutes').append(routeList)
    })
})

$('#uploadRouteButton').on('click', function(event) {
    let file = document.getElementById("routeFile").files[0];
    contents = file.text().then(contents => {
        routeName = file.name

        data = {
            Name: routeName,
            Route: contents
        }
        sendEvent('routes.new', data)
        notify("Route uploaded to Vanchor", "It should now be visible under AutoPilot Routes")

    })


})

$('#updateButton').on('click', function(event) {

    file = document.getElementById("updateFile").files[0];
    form = new FormData();

    form.append("zip", file);
    fetch('/upload/update', { method: "POST", body: form });

    $(event.target).text = "Update submitted!"
    $(event.target).disable()
    notify("Update submitted", "Update has been submitted to Vanchor")

})

$('#setStepperPosAsZero').on('click', function(event) {
    cal = 0
    sendEvent("controller.send", `CAL ${cal}`)

})