// Functions


function createRow(name, value) {
    tr = document.createElement('tr')

    [name, value].forEach(d => {
        td = document.createElement('td')
        td.innerHTML = `<b>${d}</b>`
        tr.append(td)
    })

    return tr
}

function buildStatusTable() {
    fetch('/status').then(response => {
        response.json().then(data => {

        })
    })
}

function sendCommand(name, value) {
    cmd = {
        'Command': name,
        'Value': value
    }

    console.log(cmd)
}


//
var sim = false
var status
setInterval(function() {
    if ($("#dataInfo")[0].className.includes('show') || true == true) {
        fetch('/status').then(response => {

            response.json().then(data => {

                if (sim) {
                    data.Navigation.Compass.Heading = Math.round((Math.random() * (360 - 0) + 0))
                    data.Motor.Speed = Math.round((Math.random() * (100 - 0) + 0))
                }

                if (data.Speed != $("#speed").val()) {
                    $("#speed").val(data.Motor.Speed)
                    applyFill($("#speed")[0]);
                }
                rotateObject(enginePointer, (360 - data.Stepper.Position), 150, 150)


                if (data.Functions.Vanchor.Enabled == 1) {
                    document.getElementById('statusCircle').setAttributeNS(null, "fill", "green");
                } else if (data.Functions.HoldHeading.Enabled == 1) {

                    document.getElementById('statusCircle').setAttributeNS(null, "fill", "yellow");

                } else if (data.Functions.AutoPilot.Enabled == 1) {
                    document.getElementById('statusCircle').setAttributeNS(null, "fill", "#3d3d3d");
                } else {
                    document.getElementById('statusCircle').setAttributeNS(null, "fill", "black");
                }



                rotatex(g1, data.Navigation.Heading)
                setDegrees(data.Navigation.Compass.Heading)
                setSpeed(data.Motor.Speed)

                if (data.Sonar != undefined) {
                    if (data.Sonar.Depth != undefined) {
                        drawDepth(data.Sonar.Depth)
                    }
                } else {
                    drawDepth('0.0')
                }


                document.getElementById('jsonInfo').textContent = JSON.stringify(data, undefined, 2)

                toggles = $('a[data-type=toggleFunction]')
                toggles.toArray().forEach(function(node) {
                    target = $(node)
                    func = node.attributes['data-func'].Value
                    try {
                        isEnabled = data['Functions'][func]['Enabled']

                        if (isEnabled) {
                            target.data('on', false)

                            funcName = target.data('func')
                            func = funcName.toLowerCase()

                            target.removeClass('btn-danger')
                            target.addClass('btn-success')
                            target.text(`Enable ${funcName}`)

                        } else {
                            target.data('on', true)

                            funcName = target.data('func')
                            func = funcName.toLowerCase()

                            target.addClass('btn-danger')
                            target.removeClass('btn-success')
                            target.text(`Disable ${funcName}`)
                        }
                    } catch {

                    }
                })


                return data
            })
        })
    }

}, 2000)

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

function registerSendEventButton(button) {
    console.log('Registering sendEventButton on: ')
    console.log(button)
    $(button).on('click', function(event) {

        b = $(event.target)
        event = b.data('event')
        try {
            arg = b.data('text').split(',')
        } catch {
            arg = ""
        }

        console.log(`sendEventButton: Sending event Event:${event} Arg:${arg}`)
        sendEvent(event, arg)
    })

}

$('#showAutopilot').on('click', function(event) {
    fetch('/status/Functions/AutoPilot/Routes').then(response => {
        routeList = document.createElement('ul')
        routeList.setAttribute('class', 'w-100')
        response.json().then(status => {
            status.forEach(function(val) {
                li = document.createElement('div')
                li.setAttribute('class', 'list-group w-100 mt-2')
                liRow = document.createElement('div')
                liRow.setAttribute('class', 'list-group-item-dark bg-dark w-100')

                btn = document.createElement('button')
                btn.setAttribute('data-type', 'sendSEvent')
                btn.setAttribute('class', 'btn btn-secondary btn-sm mr-2 w-75')
                btn.setAttribute('data-event', 'autopilot.startroute')
                btn.setAttribute('data-text', [val, true])
                btn.innerHTML = `Start ${val}`

                liRow.append(btn)
                registerSendEventButton($(btn))
                btn = document.createElement('button')
                btn.setAttribute('data-type', 'sendSEvent')
                btn.setAttribute('class', 'btn btn-secondary btn-sm mr-2 w-75')
                btn.setAttribute('data-event', 'autopilot.startroute')
                btn.setAttribute('data-text', [val, false])
                btn.innerHTML = `Load`

                liRow.append(btn)
                registerSendEventButton($(btn))
                li.append(liRow)

                routeList.append(li)

            })
        }).then(data => {
            $('#gpxRoutes').html('')
            $('#gpxRoutes').append(routeList)
        })
    })
})


//

$("#relativeSteeringToggle").on('click', function(event) {
    n = $(`#${event.target.id}`)
    state = n.data('state')
    if (state == true) {
        n.data('state', false)
        n.removeClass('btn-success')
        n.addClass('btn-danger')
    } else if (state == false) {
        n.data('state', true)
        n.removeClass('btn-danger')
        n.addClass('btn-success')

    }
})

$("#showDataInfo").on('click', function(event) {
    $(event.target.dataset.target).collapse('toggle')
})

$("#maxSteeringToggle").on("click", function(event) {
    n = $(`#${event.target.id}`)
    state = n.data('state')
    if (state == true) {
        n.data('state', false)
        n.removeClass('btn-danger')
        n.addClass('btn-success')
    } else if (state == false) {
        n.data('state', true)
        n.removeClass('btn-success')
        n.addClass('btn-danger')

    }

    if (state) {
        document.getElementById('steering').setAttribute('min', "90")
        document.getElementById('steering').setAttribute('max', "270")
    } else {
        document.getElementById('steering').setAttribute('min', "0")
        document.getElementById('steering').setAttribute('max', "360")
    }


})

// Data


$('#steering').on('change', function(event) {
    pos = parseInt($('#steering')[0].value)

    if (pos < 180) {
        pos = 360 - (180 - pos)
    } else {
        pos = pos - 180
    }

    if ($("#relativeSteeringToggle").data('state')) {
        sendEvent("steering.set.position", pos)
    } else {
        sendEvent("steering.set.heading", pos)
    }

    document.getElementById("currentSteering").innerHTML = `Current: ${pos}`
})



const settings = {
    fill: '#339980',
    background: '#d7dcdf'
}

// This function applies the fill to our sliders by using a linear gradient background
function applyFill(slider) {

    const percentage = 100 * (slider.value - slider.min) / (slider.max - slider.min);

    const bg = `linear-gradient(90deg, ${settings.fill} ${percentage}%, ${settings.background} ${percentage + 0.1}%)`;
    slider.style.background = bg;
}

function sendStatusEvent(event, value) {

    sendEvent("status.set", [event, value])

}

function sendEvent(event, value) {
    command = {
        "Event": event,
        "Argument": value
    }

    fetch('/command', {
        'body': JSON.stringify(command),
        'method': 'POST'
    })
}

$('#speed').on('change', function(event) {

    sendStatusEvent('Motor/Speed', parseInt(event.target.value))
    applyFill(event.target);

})

$('#motorOff').on('click', function(event) {
    sendStatusEvent("Motor/Speed", 0)

    $("#speed").val(0)

    applyFill($("#speed")[0]);
})

$('#vanchorOn').on('click', function(event) {
    sendEvent("function.vanchor.enable", true)
})

$('#vanchorOff').on('click', function(event) {
    sendEvent("function.vanchor.disable", false)
})


$('#lockHeadingOn').on('click', function(event) {

    sendEvent("function.holdheading.enable", true)

})

$('#lockHeadingOff').on('click', function(event) {
    sendEvent("function.holdheading.disable", true)
})

$('#zeroSteering').on('click', function(event) {
    if ($("#relativeSteeringToggle").data('state')) {
        sendEvent("steering.set.position", 0)
    } else {
        sendEvent("steering.set.heading", 0)
    }
})

$('#calibrateSteering').on('click', function(event) {

    sendEvent("stepper.calibrate", true)

})


//

var svgNS = "http://www.w3.org/2000/svg";

var svg = document.getElementById("compass");

var g1 = document.createElementNS(svgNS, "g");
g1.setAttribute("id", "compassBody")
var pointer = document.createElementNS(svgNS, "polygon");
pointer.setAttributeNS(null, "points", "150,0 155,12 145,12");
pointer.setAttributeNS(null, "fill", "red");
svg.appendChild(pointer);

var enginePointer = document.createElementNS(svgNS, "polygon");
enginePointer.setAttributeNS(null, "points", "150,80 155,92 145,92");
enginePointer.setAttributeNS(null, "fill", "green");
svg.appendChild(enginePointer);



var c = document.createElementNS(svgNS, "circle");
c.setAttributeNS(null, "cx", 150);
c.setAttributeNS(null, "cy", 150);
c.setAttributeNS(null, "r", 18);
c.setAttributeNS(null, "fill", "black");
c.setAttributeNS(null, "fill-opacity", 0.4);
c.setAttributeNS(null, "id", 'statusCircle');

svg.appendChild(c);

drawCenterLine(150, 190, 150, 220);
drawCenterLine(150, 80, 150, 120);

drawCenterLine(190, 150, 220, 150);
drawCenterLine(80, 150, 120, 150);

drawCardinalDirection(143, 72, "N");
drawCardinalDirection(228, 158, "E");
drawCardinalDirection(143, 242, "S");
drawCardinalDirection(58, 158, "W");

for (var i = 0; i < 360; i += 2) {
    // draw degree lines
    var s = "grey";
    if (i == 0 || i % 30 == 0) {
        w = 3;
        s = "white";
        y2 = 50;
    } else {
        w = 1;
        y2 = 45;
    }

    var l1 = document.createElementNS(svgNS, "line");
    l1.setAttributeNS(null, "x1", 150);
    l1.setAttributeNS(null, "y1", 30);
    l1.setAttributeNS(null, "x2", 150);
    l1.setAttributeNS(null, "y2", y2);
    l1.setAttributeNS(null, "stroke", s);
    l1.setAttributeNS(null, "stroke-width", w);
    l1.setAttributeNS(null, "transform", "rotate(" + i + ", 150, 150)");
    g1.appendChild(l1);

    // draw degree value every 30 degrees
    if (i % 30 == 0) {
        var t1 = document.createElementNS(svgNS, "text");
        if (i > 100) {
            t1.setAttributeNS(null, "x", 140);
        } else if (i > 0) {
            t1.setAttributeNS(null, "x", 144);
        } else {
            t1.setAttributeNS(null, "x", 147);
        }
        t1.setAttributeNS(null, "y", 24);
        t1.setAttributeNS(null, "font-size", "11px");
        t1.setAttributeNS(null, "font-family", "Helvetica");
        t1.setAttributeNS(null, "fill", "grey");
        t1.setAttributeNS(null, "style", "letter-spacing:1.0");
        t1.setAttributeNS(null, "transform", "rotate(" + i + ", 150, 150)");
        var textNode = document.createTextNode(i);
        t1.appendChild(textNode);
        g1.appendChild(t1);
    }
}

function drawDegrees(degrees) {
    var direction = document.createElementNS(svgNS, "text");
    direction.setAttributeNS(null, "x", 154);
    direction.setAttributeNS(null, "y", 157);
    direction.setAttributeNS(null, "font-size", "20px");
    direction.setAttributeNS(null, "font-family", "Helvetica");
    direction.setAttributeNS(null, "text-anchor", "middle");
    direction.setAttributeNS(null, "fill", "white");
    direction.setAttributeNS(null, "id", "degreeText");
    var textNode = document.createTextNode(`${degrees}°`);
    direction.appendChild(textNode);
    svg.appendChild(direction);
}

function setDegrees(degrees) {
    document.getElementById("degreeText").innerHTML = `${degrees}°`
}

drawDegrees(0)

function drawSpeed(speed) {
    var direction = document.createElementNS(svgNS, "text");
    direction.setAttributeNS(null, "x", 154);
    direction.setAttributeNS(null, "y", 180);
    direction.setAttributeNS(null, "font-size", "15px");
    direction.setAttributeNS(null, "font-family", "Helvetica");
    direction.setAttributeNS(null, "text-anchor", "middle");
    direction.setAttributeNS(null, "fill", "gray");
    direction.setAttributeNS(null, "id", "speedText");
    var textNode = document.createTextNode(`${speed}%`);
    direction.appendChild(textNode);
    svg.appendChild(direction);
}

function setSpeed(speed) {
    document.getElementById("speedText").innerHTML = `${speed}%`
}

drawSpeed(0)



function drawCenterLine(x1, y1, x2, y2) {
    var centreLineHorizontal = document.createElementNS(svgNS, "line");
    centreLineHorizontal.setAttributeNS(null, "x1", x1);
    centreLineHorizontal.setAttributeNS(null, "y1", y1);
    centreLineHorizontal.setAttributeNS(null, "x2", x2);
    centreLineHorizontal.setAttributeNS(null, "y2", y2);
    centreLineHorizontal.setAttributeNS(null, "stroke", "grey");
    centreLineHorizontal.setAttributeNS(null, "stroke-width", 1);
    centreLineHorizontal.setAttributeNS(null, "stroke-opacity", 0.5);
    g1.appendChild(centreLineHorizontal);
}

function drawCardinalDirection(x, y, displayText) {
    var direction = document.createElementNS(svgNS, "text");
    direction.setAttributeNS(null, "x", x);
    direction.setAttributeNS(null, "y", y);
    direction.setAttributeNS(null, "font-size", "20px");
    direction.setAttributeNS(null, "font-family", "Helvetica");
    direction.setAttributeNS(null, "font-align", "middle");
    direction.setAttributeNS(null, "fill", "gray");
    direction.setAttributeNS(null, "id", "cardinal" + displayText);
    var textNode = document.createTextNode(displayText);
    direction.appendChild(textNode);
    g1.appendChild(direction);
}

function drawDepth(depth) {
    obj = $(`#depth`)
    if (obj.length == 0) {
        var direction = document.createElementNS(svgNS, "text");
        direction.setAttributeNS(null, "id", "depth");
        direction.setAttributeNS(null, "x", 230);
        direction.setAttributeNS(null, "y", 20);
        direction.setAttributeNS(null, "font-size", "20px");
        direction.setAttributeNS(null, "font-family", "Helvetica");
        direction.setAttributeNS(null, "font-align", "middle");
        direction.setAttributeNS(null, "fill", "gray");
        var textNode = document.createTextNode(`${depth}m`);
        direction.appendChild(textNode);
        svg.appendChild(direction);
    } else {
        obj.text((`${depth}m`))
    }
}

g1.setAttribute("style", "transform-origin:center")
svg.appendChild(g1);


function rotatex(obj, degrees) {;

    obj.style.WebkitTransitionDuration = "1s";
    obj.style.WebkitTransform = `rotate(${degrees}deg)`;

    ['N', 'S', 'E', 'W'].forEach(function(val) {
        cardinal = $(`#cardinal${val}`)
        cardinal[0].style.WebkitTransitionDuration = "1s";
        cardinal[0].style.WebkitTransformOrigin = `${Number(cardinal.attr('x')) + 8}px ${Number(cardinal.attr('y')) - 10}px`
        cardinal[0].style.WebkitTransform = `rotate(${360 - degrees}deg)`;
    })

}

function rotateObject(obj, degrees, x, y) {
    obj.style.WebkitTransitionDuration = "1s";
    obj.style.WebkitTransformOrigin = `${x}px ${y}px`
    obj.style.WebkitTransform = `rotate(${360 - degrees}deg)`;
}

buttons = $('button[data-type=sendEvent]')
links = $('a[data-type=sendEvent]')
registerSendEventButton(buttons)
registerSendEventButton(links)


$('a[data-type=toggleFunction]').on('click', function(event) {
    target = $(event.target)
    if (target.data('on') == true) {
        target.data('on', false)

        funcName = target.data('func')
        func = funcName.toLowerCase()

        target.removeClass('btn-danger')
        target.addClass('btn-success')
        target.text(`Enable ${funcName}`)

    } else {
        target.data('on', true)

        funcName = target.data('func')
        func = funcName.toLowerCase()

        target.addClass('btn-danger')
        target.removeClass('btn-success')
        target.text(`Disable ${funcName}`)
    }
})