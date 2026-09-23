document.addEventListener('DOMContentLoaded', function() {
    // Navbar scroll handling
        const navbar = id('mainNavbar');

    if (navbar) {
        window.addEventListener('scroll', function() {
            if (window.scrollY > 50) {
                navbar.classList.add('scrolled');
            } else {
                navbar.classList.remove('scrolled');
            }
        });
    }

    // Set minimum date to today
    const today = new Date().toISOString().split('T')[0];
    const fechaViaje = id('fecha_viaje');
    const fechaVuelta = id('fecha_vuelta');

    if (fechaViaje && fechaVuelta) {
        fechaViaje.min = today;

        fechaViaje.addEventListener('change', function() {
            fechaVuelta.min = this.value;

            if (fechaVuelta.value && fechaVuelta.value < this.value) {
                fechaVuelta.value = this.value;
            }
        });
    }

    // Swap Origin & Destination
    const btnSwap = id('btnSwap');
    const origenInput = id('origen');
    const destinoInput = id('destino');

    if(btnSwap && origenInput && destinoInput) {
        btnSwap.addEventListener('click', function() {
            const temp = origenInput.value;
            origenInput.value = destinoInput.value;
            destinoInput.value = temp;
        });
    }

    // Form Submit Simulation
    const searchForm = id('searchForm');
    const searchToast = id('searchToast');
    const toastMessage = id('toastMessage');

    if(searchForm) {
        searchForm.addEventListener('submit', function(e) {
            const orig = origenInput.value.trim();
            const dest = destinoInput.value.trim();
            const date = fechaViaje.value;

            if(!orig || !dest || !date) {
                e.preventDefault();
                showToast("Por favor completa Origen, Destino y Fecha de Ida.");
            }
        });
    }

    function showToast(msg) {
        toastMessage.innerText = msg;
        searchToast.style.display = 'flex';
        gsap.fromTo(searchToast, { opacity: 0, y: 20 }, { opacity: 1, y: 0, duration: 0.4 });

        setTimeout(() => {
            gsap.to(searchToast, {
                opacity: 0,
                y: 20,
                duration: 0.4,
                onComplete: () => { searchToast.style.display = 'none'; }
            });
        }, 4000);
    }

    // Utility function
    function id(elemId) {
        return document.getElementById(elemId); // <-- Agregado 'return' para que funcione
    }

    // GSAP Animations: mismas secciones, más movimiento y sin alterar el layout
    const intro = gsap.timeline({
        defaults: { ease: 'power3.out' }
    });

    if (document.querySelector('.brand-mark')) {
        intro.from(
            '.brand-mark',
            {
                opacity: 0,
                scale: 0.88,
                duration: 0.55
            },
            '-=0.35'
        );
    }

    if (document.querySelector('.hero-badge')) {
        intro.from(
            '.hero-badge',
            {
                opacity: 0,
                y: -18,
                duration: 0.65
            },
            '-=0.2'
        );
    }

    if (document.querySelector('.hero-title')) {
        intro.from(
            '.hero-title',
            {
                opacity: 0,
                y: 34,
                duration: 0.85
            },
            '-=0.35'
        );
    }

    if (document.querySelector('.hero-subtitle')) {
        intro.from(
            '.hero-subtitle',
            {
                opacity: 0,
                y: 26,
                duration: 0.7
            },
            '-=0.45'
        );
    }

    if (document.querySelector('.hero-slogan-box')) {
        intro.from(
            '.hero-slogan-box',
            {
                opacity: 0,
                x: -24,
                duration: 0.65
            },
            '-=0.4'
        );
    }

    if (document.querySelector('.btn-hero-primary, .btn-hero-secondary')) {
        intro.from(
            '.btn-hero-primary, .btn-hero-secondary',
            {
                opacity: 0,
                y: 18,
                duration: 0.6,
                stagger: 0.12
            },
            '-=0.35'
        );
    }

    gsap.from('.search-card', {
        scrollTrigger: { trigger: '.search-container', start: 'top 92%', once: true },
        opacity: 0, y: 42, scale: 0.985, duration: 0.9, ease: 'power3.out'
    });

    gsap.from('.pillars-section .section-header', {
        scrollTrigger: { trigger: '.pillars-section', start: 'top 82%', once: true },
        opacity: 0, y: 30, duration: 0.75
    });
    gsap.from('.pillar-card', {
        scrollTrigger: { trigger: '.pillars-section .row', start: 'top 84%', once: true },
        opacity: 0, y: 42, duration: 0.82, stagger: 0.16, ease: 'power3.out'
    });

    gsap.from('.departures-section .section-tag, .departures-section .section-title', {
        scrollTrigger: { trigger: '.departures-section', start: 'top 82%', once: true },
        opacity: 0, x: -28, duration: 0.75, stagger: 0.1
    });
    gsap.from('.departure-row', {
        scrollTrigger: { trigger: '.terminal-board', start: 'top 84%', once: true },
        opacity: 0, x: -34, duration: 0.7, stagger: 0.12, ease: 'power2.out'
    });

    gsap.from('.destinations-section .section-header', {
        scrollTrigger: { trigger: '.destinations-section', start: 'top 82%', once: true },
        opacity: 0, y: 30, duration: 0.78
    });
    gsap.from('.destination-card', {
        scrollTrigger: { trigger: '.destinations-section .row', start: 'top 84%', once: true },
        opacity: 0, y: 44, scale: 0.985, duration: 0.82, stagger: 0.16, ease: 'power3.out'
    });

    const routePath = document.querySelector('.route-line-svg path');
    if (routePath) {
        const length = routePath.getTotalLength();
        routePath.style.strokeDasharray = length;
        routePath.style.strokeDashoffset = length;
        gsap.to(routePath, {
            scrollTrigger: { trigger: '.history-banner', start: 'top 78%', once: true },
            strokeDashoffset: 0, duration: 2.1, ease: 'power2.inOut'
        });
    }
    gsap.from('.history-bg-year', {
        scrollTrigger: { trigger: '.history-banner', start: 'top 80%', once: true },
        opacity: 0, scale: 0.9, duration: 1.2, ease: 'power2.out'
    });
    gsap.from('.history-banner .container > *', {
        scrollTrigger: { trigger: '.history-banner', start: 'top 76%', once: true },
        opacity: 0, y: 28, duration: 0.78, stagger: 0.12, ease: 'power3.out'
    });

    gsap.from('.fleet-img-container', {
        scrollTrigger: { trigger: '.fleet-card', start: 'top 82%', once: true },
        opacity: 0, x: -38, duration: 0.9, ease: 'power3.out'
    });
    gsap.from('.fleet-card .col-lg-5 > *', {
        scrollTrigger: { trigger: '.fleet-card', start: 'top 82%', once: true },
        opacity: 0, x: 28, duration: 0.72, stagger: 0.1, ease: 'power3.out'
    });

    gsap.from('.manifesto-title, .manifesto-section .lead', {
        scrollTrigger: { trigger: '.manifesto-section', start: 'top 78%', once: true },
        opacity: 0, y: 34, duration: 0.85, stagger: 0.18, ease: 'power3.out'
    });

    gsap.from('.faq-section .section-header, .accordion-item-laportena', {
        scrollTrigger: { trigger: '.faq-section', start: 'top 82%', once: true },
        opacity: 0, y: 28, duration: 0.72, stagger: 0.09, ease: 'power2.out'
    });

    gsap.from('.footer-laportena .row, .footer-copy', {
        scrollTrigger: { trigger: '.footer-laportena', start: 'top 92%', once: true },
        opacity: 0, y: 22, duration: 0.72, stagger: 0.12
    });

    // Cinematic drift muy sutil del fondo del Hero
    gsap.to('.hero-section', {
        backgroundPosition: '50% 56%',
        ease: 'none',
        scrollTrigger: { trigger: '.hero-section', start: 'top top', end: 'bottom top', scrub: 1.2 }
    });
}); // <-- ESTA ES LA LLAVE QUE FALTABA