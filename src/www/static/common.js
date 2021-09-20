//<div class="alert alert-warning alert-dismissible fade show" role="alert">
//    <strong>Holy guacamole!</strong> You should check in on some of those fields below.
//  <button type="button" class="close" data-dismiss="alert" aria-label="Close">
//        <span aria-hidden="true">&times;</span>
//    </button>
//</div>




function notify(title, message, severity = "success") {
    template = `<div class="alert alert-${severity} alert-dismissible fade show w-100" role="alert">
                    <strong class="mr-2">${title}</strong> ${message}
                    <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
                </div>`

    $('#notifications').append($(template))
}