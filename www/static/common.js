//<div class="alert alert-warning alert-dismissible fade show" role="alert">
//    <strong>Holy guacamole!</strong> You should check in on some of those fields below.
//  <button type="button" class="close" data-dismiss="alert" aria-label="Close">
//        <span aria-hidden="true">&times;</span>
//    </button>
//</div>




function create_notification(title, message, sev = "success") {
    div = document.createElement('div')

    div.setAttribute('class', `alert alert-${sev} alert-dismissible fade show`)
    div.setAttribute('role', 'alert')

    div.innerHTML = `<strong>${title}</strong> ${message}`

    button = document.createElement('button')
    button.setAttribute('type', 'button')
    button.setAttribute('class', 'btn-close')
    button.setAttribute('data-bs-dismiss', 'alert')
    button.setAttribute('aria-label', 'close')

    div.append(button)

    document.getElementById('messages').append(div)
}