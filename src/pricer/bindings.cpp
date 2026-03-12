#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include "bates.hpp"
#include "cos_pricer.hpp"
#include "mc_pricer.hpp"

namespace py = pybind11;

PYBIND11_MODULE(bates_pricer, m) {
    m.doc() = "Bates SVJ model pricer: COS method + Monte Carlo validation";

    py::class_<bates::Params>(m, "Params")
        .def(py::init<>())
        .def_readwrite("S",       &bates::Params::S,       "Spot price")
        .def_readwrite("v0",      &bates::Params::v0,      "Initial variance")
        .def_readwrite("kappa",   &bates::Params::kappa,   "Mean-reversion speed")
        .def_readwrite("theta",   &bates::Params::theta,   "Long-run variance")
        .def_readwrite("sigma_v", &bates::Params::sigma_v, "Vol of vol")
        .def_readwrite("rho",     &bates::Params::rho,     "Spot-variance correlation")
        .def_readwrite("lambda_", &bates::Params::lambda,  "Jump intensity (jumps/year)")
        .def_readwrite("mu_j",    &bates::Params::mu_j,    "Mean log-jump size")
        .def_readwrite("sigma_j", &bates::Params::sigma_j, "Std dev log-jump size")
        .def_readwrite("r",       &bates::Params::r,       "Risk-free rate")
        .def_readwrite("q",       &bates::Params::q,       "Dividend/carry yield")
        .def("__repr__", [](const bates::Params& p) {
            return "Params(S=" + std::to_string(p.S)
                 + ", v0=" + std::to_string(p.v0)
                 + ", kappa=" + std::to_string(p.kappa)
                 + ", theta=" + std::to_string(p.theta)
                 + ", sigma_v=" + std::to_string(p.sigma_v)
                 + ", rho=" + std::to_string(p.rho)
                 + ", lambda=" + std::to_string(p.lambda)
                 + ", mu_j=" + std::to_string(p.mu_j)
                 + ", sigma_j=" + std::to_string(p.sigma_j)
                 + ", r=" + std::to_string(p.r)
                 + ", q=" + std::to_string(p.q) + ")";
        });

    // COS pricer
    m.def("binary_call",
        [](double K, double T, const bates::Params& p, int N) {
            return cos_pricer::binary_call(K, T, p, N);
        },
        py::arg("K"), py::arg("T"), py::arg("params"), py::arg("N") = 256,
        "Cash-or-nothing binary call price via COS method. Returns e^{-rT} * Q(S(T) > K).");

    m.def("binary_put",
        [](double K, double T, const bates::Params& p, int N) {
            return cos_pricer::binary_put(K, T, p, N);
        },
        py::arg("K"), py::arg("T"), py::arg("params"), py::arg("N") = 256,
        "Cash-or-nothing binary put price via COS method. Returns e^{-rT} * Q(S(T) < K).");

    m.def("binary_call_prob",
        [](double K, double T, const bates::Params& p, int N) {
            return cos_pricer::binary_call_prob(K, T, p, N);
        },
        py::arg("K"), py::arg("T"), py::arg("params"), py::arg("N") = 256,
        "Undiscounted Q(S(T) > K) via COS method.");

    py::class_<cos_pricer::Greeks>(m, "Greeks")
        .def_readonly("delta",        &cos_pricer::Greeks::delta)
        .def_readonly("vega",         &cos_pricer::Greeks::vega)
        .def_readonly("theta",        &cos_pricer::Greeks::theta)
        .def_readonly("lambda_sens",  &cos_pricer::Greeks::lambda_sens)
        .def("__repr__", [](const cos_pricer::Greeks& g) {
            return "Greeks(delta=" + std::to_string(g.delta)
                 + ", vega=" + std::to_string(g.vega)
                 + ", theta=" + std::to_string(g.theta)
                 + ", lambda_sens=" + std::to_string(g.lambda_sens) + ")";
        });

    m.def("greeks",
        [](double K, double T, const bates::Params& p, int N) {
            return cos_pricer::compute_greeks(K, T, p, N);
        },
        py::arg("K"), py::arg("T"), py::arg("params"), py::arg("N") = 256,
        "Finite-difference Greeks for the binary call.");

    // Monte Carlo pricer
    py::class_<mc_pricer::McResult>(m, "McResult")
        .def_readonly("price",  &mc_pricer::McResult::price)
        .def_readonly("stderr", &mc_pricer::McResult::stderr)
        .def_readonly("paths",  &mc_pricer::McResult::paths)
        .def("__repr__", [](const mc_pricer::McResult& r) {
            return "McResult(price=" + std::to_string(r.price)
                 + ", stderr=" + std::to_string(r.stderr)
                 + ", paths=" + std::to_string(r.paths) + ")";
        });

    m.def("binary_call_mc",
        [](double K, double T, const bates::Params& p, int n_paths, int n_steps, int seed) {
            return mc_pricer::binary_call_mc(K, T, p, n_paths, n_steps, seed);
        },
        py::arg("K"), py::arg("T"), py::arg("params"),
        py::arg("n_paths") = 100000, py::arg("n_steps") = 200, py::arg("seed") = 42,
        "Binary call price via Monte Carlo (antithetic variates). Use for COS validation.");
}
