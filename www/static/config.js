config_str = '{"Serial": {"NmeaInput": {"Device": "/dev/ttyUSB0", "Baudrate": 4800, "Timeout": 1}, "Controller": {"Device": "/dev/ttyACM0", "Baudrate": 19200, "Timeout": 1, "OutputInterval": 500, "InputInterval": 700}}, "Steering": {"CalibrationOffset": 0, "Acceleration": 600, "Speed": 450, "Reversed": true, "Slack": 1, "CorrectionInterval": 2, "ChokeMotor": true}, "Motor": {"RampTime": 800, "RampDelay": 20, "Offset": 20}, "Vanchor": {"Radius": 1.2, "CorrectionDelay": 1000, "SpeedMultiplier": 50}, "History": {"MaxItems": 100}, "Logging": {"Level": 10, "Format": "%(asctime)s - %(levelname)s - %(lineno)s - %(name)s:%(funcName)s - %(threadName)s - %(message)s"},"NmeaNet": {"Port": 10000}}'
config = JSON.parse(config_str)
formList = []

function draw_config_part(part, value, c) {
    n = c.join(".")
    c++

    row = document.createElement('div')
    row.setAttribute('class', 'd-grip gap-1 d-md-flex')

    //<label for="vanchorRadius" class="form-label">Vanchor radius</label>
    /*
    labelCol = document.createElement('div')
    labelCol.setAttribute('class', 'mt-0')

    inputCol = document.createElement('div')
    inputCol.setAttribute('class', 'mt-0')
    */
    label = document.createElement('p')
    label.setAttribute('class', 'h6 mb-0 pb-0')
    label.innerHTML = n + ":"
        //labelCol.append(label)


    //  <input type="number" id="vanchorRadius"></input>
    input = document.createElement('input')
    input.setAttribute('id', n.replaceAll(".", "-"))
    input.setAttribute('class', "form-control w-100")
    input.setAttribute('value', value)
    input.setAttribute('data-path', n)

    if (typeof value === "number") {
        input.setAttribute('type', 'number')
    }

    console.log(`Adding ${part}`)

    //row.append(document.createElement('br'))
    row.append(label)
    row.append(input)
        //inputCol.append(document.createElement('br'))
        //inputCol.append(input)


    //row.append(labelCol)
    //row.append(inputCol)
    $('#config').append(row)

    formList.push(n.replaceAll(".", "-"))

}

var configCount = 0

function draw_config(config, path = "Config", chain = [], i = 3) {
    configCount++


    title = document.createElement('p')
    title.setAttribute('class', `h${i} mb-3`)
    title.innerHTML = path

    console.log(path)
    if (configCount != 1) {
        $('#config').append(document.createElement('hr'))
    } else {
        title.setAttribute('class', 'h1 mt-3')
    }

    $('#config').append(title)
        //$('#config').append(document.createElement('hr'))

    Object.getOwnPropertyNames(config).forEach(function(part) {
        if (typeof config[part] === 'object') {
            console.log(`drawing config ${path}`)
            draw_config(config[part], [path, part].join("."), [path, part], i + 1)
        } else {
            new_chain = chain
            new_chain.push(part)
            console.log(`drawing parts ${path}`)
            draw_config_part(part, config[part], [path, part])
        }

    })

}

fetch("/getConfig").then(data => {
    data.json().then(config => {
        draw_config(config)


        formList.forEach(function(n) {
            $(`#${n}`).on('change', function(event) {



                path = event.target.attributes['data-path'].value.replaceAll(/^Config\./, "").replace("\.", "/")
                value = event.target.value

                console.log(path, value)

                fetch(
                    "/setConfig", {
                        "method": "POST",
                        "body": JSON.stringify({
                            "Path": path,
                            "Value": value
                        })
                    }
                ).then(data => {
                    if (data.status == 200) {
                        create_notification(`${path} was set to ${value}`, "Don't forget to save!")
                    } else {
                        create_notification(`An error occured while setting config value ${Path}`, 'See logs for more info', 'danger')
                    }
                })
            })
        })
    })
})


$('#saveBtn').on('click', function(event) {
    fetch(
        "/saveConfig", {
            "method": "POST",
            "body": ""
        }
    ).then(data => {
        if (data.status == 200) {
            create_notification(`Configuration was saved`, "Please restart")
        } else {
            create_notification(`Failed to save configuration`, "Please review the logs", 'danger')
        }
    })
})


$('#reloadBtn').on('click', function(event) {
    fetch(
        "/reload", {
            "method": "POST",
            "body": ""
        }
    ).then(data => {
        if (data.status == 200) {
            create_notification(`Values were reloaded`, "Please restart")
        } else {
            create_notification(`Failed to save configuration`, "Please review the logs", 'danger')
        }
    })
})