import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthForm } from "@/components/auth-form";
import { PrivateShell } from "@/components/private-shell";
import { SessionProvider } from "@/lib/auth/session";
import { NO_CONTENT, USER, errorResponse, jsonResponse, mockApi } from "@/test/api-mock";
import { resetRouter, router } from "@/test/navigation";

vi.mock("next/navigation", () => ({ useRouter: () => router }));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

beforeEach(() => {
  resetRouter();
  vi.unstubAllGlobals();
});

function renderWith(ui: React.ReactNode) {
  return render(<SessionProvider>{ui}</SessionProvider>);
}

const anonymous = () => errorResponse(401, "not_authenticated", "Autenticación requerida.");

describe("ruta privada", () => {
  it("redirige a /login cuando no hay sesión y no muestra el contenido", async () => {
    mockApi({ "GET /api/v1/auth/me": anonymous });

    renderWith(
      <PrivateShell>
        <p>Documentos secretos</p>
      </PrivateShell>,
    );

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
    expect(screen.queryByText("Documentos secretos")).not.toBeInTheDocument();
  });

  it("muestra el contenido y el usuario cuando hay sesión", async () => {
    mockApi({ "GET /api/v1/auth/me": () => jsonResponse(200, USER) });

    renderWith(
      <PrivateShell>
        <p>Documentos secretos</p>
      </PrivateShell>,
    );

    expect(await screen.findByText("Documentos secretos")).toBeInTheDocument();
    expect(screen.getByText(USER.email)).toBeInTheDocument();
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("no expulsa si no se pudo comprobar la sesión: avisa y permite reintentar", async () => {
    let attempts = 0;
    mockApi({
      "GET /api/v1/auth/me": () => {
        attempts += 1;
        return attempts === 1 ? errorResponse(503, "unavailable", "Caído.") : jsonResponse(200, USER);
      },
    });

    renderWith(
      <PrivateShell>
        <p>Contenido</p>
      </PrivateShell>,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo comprobar tu sesión");
    expect(router.replace).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Reintentar" }));

    expect(await screen.findByText("Contenido")).toBeInTheDocument();
  });

  it("cierra la sesión y vuelve a /login", async () => {
    const { calls } = mockApi({
      "GET /api/v1/auth/me": () => jsonResponse(200, USER),
      "POST /api/v1/auth/logout": NO_CONTENT,
    });

    renderWith(
      <PrivateShell>
        <p>Contenido</p>
      </PrivateShell>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Cerrar sesión" }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/v1/auth/logout")).toBe(true);
    expect(screen.queryByText("Contenido")).not.toBeInTheDocument();
  });

  it("si el cierre de sesión falla, sigue dentro y lo dice", async () => {
    mockApi({
      "GET /api/v1/auth/me": () => jsonResponse(200, USER),
      "POST /api/v1/auth/logout": () => errorResponse(500, "internal_error", "Error interno del servidor."),
    });

    renderWith(
      <PrivateShell>
        <p>Contenido</p>
      </PrivateShell>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Cerrar sesión" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Error interno del servidor.");
    expect(screen.getByText("Contenido")).toBeInTheDocument();
    expect(router.replace).not.toHaveBeenCalled();
  });
});

describe("login", () => {
  it("entra con credenciales válidas y va a los documentos", async () => {
    const { calls } = mockApi({
      "GET /api/v1/auth/me": anonymous,
      "POST /api/v1/auth/login": () => jsonResponse(200, USER),
    });

    renderWith(<AuthForm mode="login" />);
    await userEvent.type(screen.getByLabelText("Email"), "ana@example.com");
    await userEvent.type(screen.getByLabelText("Contraseña"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Entrar" }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/documents"));
    expect(calls.find((c) => c.path === "/api/v1/auth/login")?.body).toEqual({
      email: "ana@example.com",
      password: "correct horse battery",
    });
  });

  it("muestra el error de credenciales sin salir de la página", async () => {
    mockApi({
      "GET /api/v1/auth/me": anonymous,
      "POST /api/v1/auth/login": () =>
        errorResponse(401, "invalid_credentials", "Email o contraseña incorrectos."),
    });

    renderWith(<AuthForm mode="login" />);
    await userEvent.type(screen.getByLabelText("Email"), "ana@example.com");
    await userEvent.type(screen.getByLabelText("Contraseña"), "mala contraseña");
    await userEvent.click(screen.getByRole("button", { name: "Entrar" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Email o contraseña incorrectos.");
    expect(router.replace).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Entrar" })).toBeEnabled();
  });

  it("impide el doble envío mientras la petición está en curso", async () => {
    let release: (r: Response) => void = () => {};
    const { calls } = mockApi({
      "GET /api/v1/auth/me": anonymous,
      "POST /api/v1/auth/login": () => new Promise<Response>((resolve) => (release = resolve)),
    });

    renderWith(<AuthForm mode="login" />);
    await userEvent.type(screen.getByLabelText("Email"), "ana@example.com");
    await userEvent.type(screen.getByLabelText("Contraseña"), "correct horse battery");
    const submit = screen.getByRole("button", { name: "Entrar" });
    await userEvent.click(submit);
    await userEvent.click(screen.getByRole("button", { name: "Entrando…" }));

    expect(calls.filter((c) => c.path === "/api/v1/auth/login")).toHaveLength(1);
    release(jsonResponse(200, USER));
    await waitFor(() => expect(router.replace).toHaveBeenCalled());
  });

  it("quien ya tiene sesión es llevado a los documentos", async () => {
    mockApi({ "GET /api/v1/auth/me": () => jsonResponse(200, USER) });

    renderWith(<AuthForm mode="login" />);

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/documents"));
  });
});

describe("registro", () => {
  it("crea la cuenta, abre sesión y va a los documentos", async () => {
    const { calls } = mockApi({
      "GET /api/v1/auth/me": anonymous,
      "POST /api/v1/auth/register": () => jsonResponse(201, USER),
      "POST /api/v1/auth/login": () => jsonResponse(200, USER),
    });

    renderWith(<AuthForm mode="register" />);
    await userEvent.type(screen.getByLabelText("Email"), "ana@example.com");
    await userEvent.type(screen.getByLabelText("Contraseña"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Crear cuenta" }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/documents"));
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual([
      "/api/v1/auth/register",
      "/api/v1/auth/login",
    ]);
  });

  it("muestra el error de un email ya registrado", async () => {
    mockApi({
      "GET /api/v1/auth/me": anonymous,
      "POST /api/v1/auth/register": () =>
        errorResponse(409, "email_already_registered", "Ya existe una cuenta con ese email."),
    });

    renderWith(<AuthForm mode="register" />);
    await userEvent.type(screen.getByLabelText("Email"), "ana@example.com");
    await userEvent.type(screen.getByLabelText("Contraseña"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Crear cuenta" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Ya existe una cuenta con ese email.");
  });

  it("asocia los errores de validación al campo correspondiente", async () => {
    mockApi({
      "GET /api/v1/auth/me": anonymous,
      "POST /api/v1/auth/register": () =>
        errorResponse(422, "validation_error", "Los datos enviados no son válidos.", [
          { loc: ["body", "password"], message: "La contraseña es demasiado corta.", type: "value_error" },
        ]),
    });

    renderWith(<AuthForm mode="register" />);
    await userEvent.type(screen.getByLabelText("Email"), "ana@example.com");
    await userEvent.type(screen.getByLabelText("Contraseña"), "corta");
    await userEvent.click(screen.getByRole("button", { name: "Crear cuenta" }));

    const password = screen.getByLabelText("Contraseña");
    await waitFor(() => expect(password).toHaveAttribute("aria-invalid", "true"));
    expect(screen.getByText("La contraseña es demasiado corta.")).toBeInTheDocument();
  });
});
