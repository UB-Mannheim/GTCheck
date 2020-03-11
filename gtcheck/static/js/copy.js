$( document ).ready(function() {
		var copyTextareaBtn = document.querySelector('.js-textareacopybtn');
		copyTextareaBtn.addEventListener('click', function(event) {
		var copyTextarea = document.querySelector('.js-copytextarea');
		console.log(copyTextarea);
		copyTextarea.select();

		try {
			var successful = document.execCommand('copy');
		}
		catch (err) {
			console.log('Unable to copy');
		}
	});
});